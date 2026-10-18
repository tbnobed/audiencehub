"""Audited first-party resources and licensed enrichment metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from app.canonical import FIELDS

revision = "0032_zeta_schema"
down_revision = "0031_identity_write_guard"
branch_labels = None
depends_on = None


def upgrade():
    # A database catalog freezes this revision's accepted first-party names.
    op.create_table("canonical_field_catalog",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False))
    for key, meta in FIELDS.items():
        op.execute(sa.text("INSERT INTO canonical_field_catalog VALUES (:name,:typ,:sensitive)").bindparams(
            name=key, typ=meta["data_type"], sensitive=meta["sensitive"]))
    op.execute("""
      CREATE FUNCTION kin_canonical_attributes() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE conflict text; normalized jsonb;
      BEGIN
        SELECT jsonb_object_agg(name,value),max(CASE WHEN variants>1 THEN name END)
        INTO normalized,conflict FROM (
          SELECT coalesce(c.name,j.key) AS name,(jsonb_agg(j.value)->0) AS value,
                 count(DISTINCT j.value) AS variants
          FROM jsonb_each(NEW.attributes) j LEFT JOIN canonical_field_catalog c
            ON replace(c.name,'_','')=regexp_replace(lower(j.key),'[^a-z0-9]','','g')
          GROUP BY coalesce(c.name,j.key)
        ) mapped;
        IF conflict IS NOT NULL THEN
          RAISE EXCEPTION 'Conflicting canonical aliases for %; no value discarded',conflict;
        END IF;
        NEW.attributes := coalesce(normalized,'{}'::jsonb);
        RETURN NEW;
      END $$;
      CREATE TRIGGER kin_canonical_attributes BEFORE INSERT OR UPDATE OF attributes ON source_records
        FOR EACH ROW EXECUTE FUNCTION kin_canonical_attributes();
      UPDATE source_records SET attributes=attributes
        WHERE EXISTS (SELECT 1 FROM jsonb_object_keys(attributes) k JOIN canonical_field_catalog c
          ON replace(c.name,'_','')=regexp_replace(lower(k),'[^a-z0-9]','','g') WHERE k<>c.name);
    """)
    for table in ("source_codes", "products", "vod_titles"):
        op.create_table(table,
            sa.Column("key", sa.Text(), primary_key=True),
            sa.Column("name", sa.Text()),
            sa.Column("series", sa.Text()),
            sa.Column("attributes", JSONB(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
        op.create_index("ix_" + table + "_series", table, ["series"])
    for table in ("events", "gifts"):
        payload = "properties" if table == "events" else "attributes"
        for col, resource in (("source_code", "source_codes"), ("product_id", "products"), ("vod_title_id", "vod_titles")):
            op.add_column(table, sa.Column(col, sa.Text()))
            # Legacy payloads remain intact; references are bound on new writes.
            # Never invent placeholder resources to satisfy old payloads.
            op.create_foreign_key(f"fk_{table}_{col}", table, resource, [col], ["key"])
            op.create_index(f"ix_{table}_{col}", table, [col])
        op.execute(f"""
          CREATE FUNCTION kin_{table}_resources() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            NEW.source_code := NEW.{payload}->>'source_code';
            NEW.product_id := NEW.{payload}->>'product_id';
            NEW.vod_title_id := NEW.{payload}->>'vod_title_id';
            RETURN NEW;
          END $$;
          CREATE TRIGGER kin_{table}_resources BEFORE INSERT OR UPDATE OF {payload} ON {table}
            FOR EACH ROW EXECUTE FUNCTION kin_{table}_resources();
        """)
    for name, typ in (("vendor", sa.Text()), ("license_id", sa.Text()), ("append_date", sa.Date())):
        op.add_column("enrichment_values", sa.Column(name, typ))
    op.add_column("enrichment_values", sa.Column("sensitive", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("ix_enrichment_sensitive_keys", "enrichment_values", ["source_id", "attribute_key"],
                    postgresql_where=sa.text("sensitive"))
    op.execute("""
      CREATE FUNCTION kin_enrichment_sensitive() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF lower(NEW.attribute_key) ~ '(religio|politic|military|veteran)' THEN
          NEW.sensitive := true;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER kin_enrichment_sensitive BEFORE INSERT OR UPDATE ON enrichment_values
        FOR EACH ROW EXECUTE FUNCTION kin_enrichment_sensitive();
      UPDATE enrichment_values SET sensitive=true
        WHERE lower(attribute_key) ~ '(religio|politic|military|veteran)';
    """)


def downgrade():
    op.execute("DROP TRIGGER kin_canonical_attributes ON source_records; DROP FUNCTION kin_canonical_attributes()")
    op.drop_table("canonical_field_catalog")
    op.execute("DROP TRIGGER kin_enrichment_sensitive ON enrichment_values; DROP FUNCTION kin_enrichment_sensitive()")
    op.drop_index("ix_enrichment_sensitive_keys", table_name="enrichment_values")
    for name in ("vendor", "license_id", "append_date", "sensitive"):
        op.drop_column("enrichment_values", name)
    for table in ("events", "gifts"):
        op.execute(f"DROP TRIGGER kin_{table}_resources ON {table}; DROP FUNCTION kin_{table}_resources()")
        for col in ("source_code", "product_id", "vod_title_id"):
            op.drop_column(table, col)
    for table in ("source_codes", "products", "vod_titles"):
        op.drop_table(table)
