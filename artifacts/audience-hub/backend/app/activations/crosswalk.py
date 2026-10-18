"""PII crosswalk exports use the already consent/suppression-filtered work set."""
import csv
import os
from sqlalchemy import text
from app.activations import normalization, service

COLUMNS = ["master_id", "key_type", "key_value", "status", "updated_at"]
ALIAS_COLUMNS = ["retired_id", "survivor_id", "merged_at"]


def export(db, run):
    directory = service.run_directory(run.id)
    outputs = []
    queries = [
        (directory / "crosswalk.csv", COLUMNS, """
          SELECT x.id,x.master_id,x.key_type,x.key_value,x.status,x.last_seen_at AS updated_at
          FROM identity_xref x JOIN active_profiles p ON p.master_id=x.master_id
          JOIN activation_work w ON w.profile_id=p.id
          WHERE x.id>:after ORDER BY x.id LIMIT 500""", "id", 0),
        (directory / "aliases.csv", ALIAS_COLUMNS, """
          SELECT a.retired_id,a.survivor_id,a.merged_at
          FROM master_id_aliases a JOIN active_profiles p ON p.master_id=a.survivor_id
          JOIN activation_work w ON w.profile_id=p.id
          WHERE a.retired_id>:after ORDER BY a.retired_id LIMIT 500""", "retired_id", ""),
    ]
    # Write-ahead field-name-only journal, committed before any remote delivery.
    from sqlalchemy.orm import Session
    with Session(db.get_bind()) as logging:
        service.audit(logging, "export.crosswalk", "activation_run", run.id, run.user_id,
                      fields=COLUMNS, alias_fields=ALIAS_COLUMNS, approved_purpose=True)
        logging.commit()
    for path, columns, sql, cursor, after in queries:
        count = 0
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
                       "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            while True:
                rows = db.execute(text(sql), {"after": after}).mappings().all()
                if not rows:
                    break
                for row in rows:
                    writer.writerow([normalization.csv_safe(row[c]) for c in columns])
                    count += 1
                after = rows[-1][cursor]
            stream.flush()
            os.fsync(stream.fileno())
        outputs.append((path, count))
    # These PII files deliberately have no ordinary single-file download path.
    run.profiles_sent = db.scalar(text("SELECT count(*) FROM activation_work"))
    return outputs
