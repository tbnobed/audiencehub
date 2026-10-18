"""Durable admin exit-migration sessions and resumable upload offsets."""
from alembic import op

revision = "0039_zeta_migrations"
down_revision = "0038_refresh_profile_views"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE zeta_migrations (
        id bigserial PRIMARY KEY, source_id bigint NOT NULL REFERENCES sources(id),
        name text NOT NULL, status text NOT NULL DEFAULT 'draft',
        audience_counts jsonb NOT NULL DEFAULT '{}', parity jsonb,
        job_id bigint REFERENCES jobs(id) ON DELETE SET NULL, batch_id bigint,
        progress jsonb NOT NULL DEFAULT '{}', error text,
        created_by bigint REFERENCES users(id), created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE zeta_migration_files (
        id bigserial PRIMARY KEY, migration_id bigint NOT NULL REFERENCES zeta_migrations(id) ON DELETE CASCADE,
        filename text NOT NULL, size bigint NOT NULL CHECK(size>=0),
        offset_bytes bigint NOT NULL DEFAULT 0 CHECK(offset_bytes>=0 AND offset_bytes<=size),
        fingerprint text NOT NULL DEFAULT '', target_type text NOT NULL DEFAULT 'auto',
        status text NOT NULL DEFAULT 'uploading', file_path text NOT NULL,
        manifests jsonb NOT NULL DEFAULT '[]', prepared_chunks integer NOT NULL DEFAULT 0,
        remote_path text, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(migration_id,filename,fingerprint)
      );
      CREATE INDEX ix_zeta_migrations_created ON zeta_migrations(created_at DESC);
      CREATE INDEX ix_zeta_files_session ON zeta_migration_files(migration_id);
    """)


def downgrade():
    op.execute("DROP TABLE zeta_migration_files; DROP TABLE zeta_migrations")
