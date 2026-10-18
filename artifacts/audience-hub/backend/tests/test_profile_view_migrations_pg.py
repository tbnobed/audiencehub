"""Isolated server-side regression tests; never run against production."""
from sqlalchemy import text
from test_consent_ingestion_pg import db
from app.migration_views import (
    refresh_profile_views, relation_columns, version_apply_guard,
)


def test_chain_exposes_all_current_profile_columns(db):
    assert relation_columns(db, "profiles") == relation_columns(db, "active_profiles")
    columns = {row[0] for row in relation_columns(db, "active_profiles")}
    assert {"master_id", "last_donation_at"} <= columns


def test_refresh_preserves_filter_dependent_views_matview_indexes_and_grants(db):
    db.execute(text("""
        INSERT INTO profiles(id,is_deleted,last_donation_at) OVERRIDING SYSTEM VALUE
          VALUES(1,false,'2026-10-17'),(2,true,NULL);
        CREATE VIEW profile_view_child WITH (security_barrier=true) AS
          SELECT id,last_donation_at FROM active_profiles WHERE id=1;
        CREATE MATERIALIZED VIEW profile_view_summary AS
          SELECT id,last_donation_at FROM profile_view_child;
        CREATE UNIQUE INDEX profile_view_summary_id ON profile_view_summary(id);
        CREATE VIEW profile_trait_child AS
          SELECT profile_id,attr_has_active_email FROM profile_traits;
        COMMENT ON VIEW profile_view_child IS 'keep child comment';
        COMMENT ON COLUMN profile_view_child.id IS 'keep column comment';
        GRANT SELECT ON profile_view_child TO PUBLIC;
    """))
    guard = version_apply_guard(db.connection())
    db.execute(text("ALTER TABLE profiles ADD COLUMN future_view_column bigint"))
    guard(ctx=None)
    assert relation_columns(db, "profiles") == relation_columns(db, "active_profiles")
    assert list(db.scalars(text("SELECT id FROM active_profiles ORDER BY id"))) == [1]
    assert list(db.scalars(text("SELECT id FROM profile_view_summary"))) == [1]
    assert db.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname=current_schema()
          AND indexname='profile_view_summary_id')
    """))
    assert db.scalar(text("""
        SELECT obj_description(to_regclass('profile_view_child'),'pg_class')
    """)) == "keep child comment"
    assert db.scalar(text("""
        SELECT c.reloptions FROM pg_class c WHERE c.oid=to_regclass('profile_view_child')
    """)) == ["security_barrier=true"]
    assert db.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_class c
          CROSS JOIN LATERAL aclexplode(c.relacl) x
          WHERE c.oid=to_regclass('profile_view_child') AND x.grantee=0
            AND x.privilege_type='SELECT')
    """))


def test_guard_handles_trait_changes_and_keeps_unpopulated_matview(db):
    db.execute(text("""
        CREATE MATERIALIZED VIEW unpopulated_traits AS
          SELECT profile_id FROM profile_traits WITH NO DATA;
        CREATE UNIQUE INDEX unpopulated_traits_id ON unpopulated_traits(profile_id);
    """))
    guard = version_apply_guard(db.connection())
    before = db.scalar(text("SELECT 'unpopulated_traits'::regclass::oid"))
    db.execute(text("ALTER TABLE profile_traits ADD COLUMN future_trait_column numeric"))
    guard(ctx=None)
    after = db.scalar(text("SELECT 'unpopulated_traits'::regclass::oid"))
    assert after != before
    assert db.scalar(text("""
        SELECT relispopulated FROM pg_class WHERE oid=to_regclass('unpopulated_traits')
    """)) is False
    assert db.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname=current_schema()
          AND indexname='unpopulated_traits_id')
    """))


def test_refresh_leaves_indexed_profile_table_data_unchanged(db):
    db.execute(text("""
        INSERT INTO profiles(id,last_donation_at) OVERRIDING SYSTEM VALUE
          VALUES(1,'2026-10-17');
    """))
    before = db.execute(text("SELECT id,last_donation_at FROM profiles")).all()
    refresh_profile_views(db.connection())
    assert db.execute(text("SELECT id,last_donation_at FROM profiles")).all() == before
