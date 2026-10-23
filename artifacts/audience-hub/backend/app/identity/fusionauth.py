"""Existing-person-only Okta -> FusionAuth CSV linking. Never invoke a resolver."""
import csv
import os
from collections import Counter
from pathlib import Path

from sqlalchemy import text

from app.activations.normalization import csv_safe
from app.identity.locking import acquire_resolver_lock
from app.keyring import pii_hashes
from app.privacy.service import audit


def import_mapping(db, input_path, report_path):
    """Caller commits. Report all outcomes, including unmatched/ambiguous rows.

    One locked set-based pass over a complete temporary staging table means
    contradictory rows anywhere in a file cannot win by file/batch order.
    The report is a sensitive local operator artifact, created with mode 0600.
    """
    if Path(input_path).resolve() == Path(report_path).resolve():
        raise ValueError("Input and report paths must differ")
    acquire_resolver_lock(db)
    db.execute(text("""
      DROP TABLE IF EXISTS pg_temp.fa_results,pg_temp.fa_candidates,pg_temp.fa_owners,pg_temp.fa_mapping;
      CREATE TEMP TABLE fa_mapping(
        row_number bigint PRIMARY KEY,okta text,fusion text,invalid boolean NOT NULL,
        okta_hashes text[],fusion_hashes text[]
      ) ON COMMIT DROP
    """))
    raw = db.connection().connection.driver_connection
    with open(input_path, newline="", encoding="utf-8-sig") as incoming:
        reader = csv.DictReader(incoming)
        if (not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames))
                or not {"okta_customer_id", "fusionauth_user_id"} <= set(reader.fieldnames)):
            raise ValueError("CSV requires unique okta_customer_id and fusionauth_user_id headers")
        with raw.cursor() as cursor:
            with cursor.copy("COPY fa_mapping FROM STDIN") as copy:
                for number, row in enumerate(reader, 2):
                    okta = (row.get("okta_customer_id") or "").strip()
                    fusion = (row.get("fusionauth_user_id") or "").strip()
                    invalid = (None in row or not okta or not fusion
                               or len(okta) > 512 or len(fusion) > 512
                               or "\x00" in okta or "\x00" in fusion)
                    # Retain invalid rows for reporting, never as SQL keys.
                    okta, fusion = okta.replace("\x00", ""), fusion.replace("\x00", "")
                    copy.write_row((number, okta, fusion, invalid,
                                    pii_hashes(okta), pii_hashes(fusion)))
    db.execute(text("""
      CREATE INDEX ON fa_mapping(okta);
      CREATE INDEX ON fa_mapping(fusion);
      ANALYZE fa_mapping;
      CREATE TEMP TABLE fa_owners ON COMMIT DROP AS
        SELECT m.okta,min(p.master_id) AS master_id,count(DISTINCT p.master_id) AS owners
        FROM (SELECT DISTINCT okta FROM fa_mapping WHERE NOT invalid) m
        LEFT JOIN identity_xref x ON x.key_type='okta_customer_id' AND x.key_value=m.okta
        LEFT JOIN active_profiles p ON p.master_id=x.master_id GROUP BY m.okta;
      CREATE UNIQUE INDEX ON fa_owners(okta);
      CREATE TEMP TABLE fa_candidates ON COMMIT DROP AS
        SELECT m.*,o.master_id,o.owners FROM fa_mapping m LEFT JOIN fa_owners o USING(okta);
      CREATE INDEX ON fa_candidates(master_id);
      CREATE INDEX ON fa_candidates(fusion);
      ANALYZE fa_candidates;
      CREATE TEMP TABLE fa_results ON COMMIT DROP AS
        WITH fusion_conflicts AS (
          SELECT fusion FROM fa_candidates WHERE NOT invalid AND owners=1
          GROUP BY fusion HAVING count(DISTINCT master_id)>1
        ), master_conflicts AS (
          SELECT master_id FROM fa_candidates WHERE NOT invalid AND owners=1
          GROUP BY master_id HAVING count(DISTINCT fusion)>1
        )
        SELECT c.row_number,c.okta,c.fusion,c.master_id,
          CASE WHEN c.invalid THEN 'invalid'
          WHEN EXISTS(SELECT 1 FROM suppressions s WHERE
            (s.type='okta_customer_id' AND s.value_hash=ANY(c.okta_hashes)) OR
            (s.type='fusionauth_user_id' AND s.value_hash=ANY(c.fusion_hashes)))
            OR EXISTS(SELECT 1 FROM identifier_blocklist b WHERE
              (b.type='okta_customer_id' AND b.value=c.okta) OR
              (b.type='fusionauth_user_id' AND b.value=c.fusion)) THEN 'suppressed'
          WHEN COALESCE(c.owners,0)=0 THEN 'unmatched'
          WHEN c.owners<>1 THEN 'ambiguous_okta'
          WHEN EXISTS(SELECT 1 FROM fusion_conflicts f WHERE f.fusion=c.fusion)
            OR EXISTS(SELECT 1 FROM master_conflicts f WHERE f.master_id=c.master_id)
            OR EXISTS(SELECT 1 FROM identity_xref x
              WHERE x.key_type='fusionauth_user_id'
                AND ((x.key_value=c.fusion AND x.master_id<>c.master_id)
                  OR (x.master_id=c.master_id AND x.key_value<>c.fusion)))
            THEN 'conflict'
          WHEN EXISTS(SELECT 1 FROM identity_xref x WHERE x.key_type='fusionauth_user_id'
            AND x.key_value=c.fusion AND x.master_id=c.master_id) THEN 'already_linked'
          ELSE 'linked' END AS status FROM fa_candidates c;
      INSERT INTO identity_xref(master_id,key_type,key_value,source_id,rule)
        SELECT DISTINCT master_id,'fusionauth_user_id',fusion,0,'okta_fusionauth_mapping'
        FROM fa_results WHERE status='linked'
        ON CONFLICT(master_id,key_type,key_value,source_id) DO NOTHING;
    """))
    counts = Counter(dict(db.execute(text(
        "SELECT status,count(*) FROM fa_results GROUP BY status")).all()))
    # Fail before commit if report cannot be created. Never overwrite a prior
    # report (or follow a symlink), and do not print raw identities to logs.
    fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["row", "okta_customer_id", "fusionauth_user_id", "master_id", "status"])
        rows = db.execute(text(
            "SELECT row_number,okta,fusion,master_id,status FROM fa_results ORDER BY row_number"),
            execution_options={"stream_results": True})
        for row in rows:
            writer.writerow([csv_safe(str(value)) if value is not None else "" for value in row])
        output.flush()
        os.fsync(output.fileno())
    audit(db, None, "identity.fusionauth.mapping", "identity", None, dict(counts))
    return dict(counts)


def command(args):
    import json
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.identity.locking import IdentityResolutionDeferred
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with Session(engine) as db:
            counts = import_mapping(db, args.mapping_file, args.report)
            db.commit()
    except (ValueError, OSError, csv.Error, IdentityResolutionDeferred) as exc:
        raise SystemExit(f"Mapping not committed: {exc}") from None
    except SQLAlchemyError:
        raise SystemExit("Mapping commit not confirmed. Check database availability/constraints, "
                         "then rerun with a new report filename; linking is idempotent.") from None
    print(json.dumps({"committed": True, "rows": counts, "report": args.report}))
