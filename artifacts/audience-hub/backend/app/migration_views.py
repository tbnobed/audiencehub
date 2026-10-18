"""Transactional refresh of profile-backed views after schema changes.

PostgreSQL expands SELECT * when a view is created, not when it is queried.
Never use DROP CASCADE: unhandled dependencies must abort the transaction.
"""
from sqlalchemy import text


def relation_columns(connection, name):
    return tuple(connection.execute(text("""
        SELECT a.attname,a.atttypid,a.atttypmod,a.attcollation
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND c.relname=:name
          AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """), {"name": name}).tuples())


def profile_signature(connection):
    return tuple((name, relation_columns(connection, name))
                 for name in ("profiles", "profile_traits"))


def refresh_profile_views(connection):
    """Snapshot/recreate dependent views in order, including matview indexes.

    active_profiles is the application's full-row view. Other catalog-discovered
    views keep their explicit projections/filters, rather than guessing that a
    deliberately selected subset should expose additional sensitive columns.
    """
    quote = connection.dialect.identifier_preparer.quote
    qualified = lambda schema, name: f"{quote(schema)}.{quote(name)}"
    rows = connection.execute(text("""
        WITH RECURSIVE dependencies(oid) AS (
          SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname=current_schema() AND c.relname IN ('profiles','profile_traits')
          UNION
          SELECT r.ev_class FROM dependencies parent
          JOIN pg_depend d ON d.refclassid='pg_class'::regclass AND d.refobjid=parent.oid
          JOIN pg_rewrite r ON d.classid='pg_rewrite'::regclass AND r.oid=d.objid
          JOIN pg_class child ON child.oid=r.ev_class AND child.relkind IN ('v','m')
          WHERE r.ev_class<>parent.oid
        )
        SELECT c.oid,n.nspname,c.relname,c.relkind,c.relispopulated,c.reloptions,
               pg_get_userbyid(c.relowner) AS owner,pg_get_viewdef(c.oid,true) AS definition,
               obj_description(c.oid,'pg_class') AS comment,t.spcname AS tablespace,
               am.amname AS access_method
        FROM dependencies dep JOIN pg_class c ON c.oid=dep.oid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_tablespace t ON t.oid=c.reltablespace
        LEFT JOIN pg_am am ON am.oid=c.relam
        WHERE c.relkind IN ('v','m')
    """)).mappings().all()
    views = {row["oid"]: dict(row) for row in rows}
    if not views:
        if relation_columns(connection, "profiles"):
            raise RuntimeError("profiles exists without active_profiles; explicit view creation required.")
        return
    previous_timeout = connection.scalar(text("SHOW lock_timeout"))
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    ids = list(views)
    edges = connection.execute(text("""
        SELECT DISTINCT r.ev_class AS child,d.refobjid AS parent
        FROM pg_rewrite r JOIN pg_depend d ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
        WHERE d.refclassid='pg_class'::regclass
          AND r.ev_class=ANY(CAST(:ids AS oid[]))
          AND d.refobjid=ANY(CAST(:ids AS oid[])) AND r.ev_class<>d.refobjid
    """), {"ids": ids}).all()
    pending = {oid: {parent for child, parent in edges if child == oid} for oid in ids}
    ordered = []
    while pending:
        ready = sorted(oid for oid, parents in pending.items() if not parents)
        if not ready:
            raise RuntimeError("Cyclic profile view dependencies; no views were dropped.")
        ordered.extend(ready)
        for oid in ready:
            del pending[oid]
        for parents in pending.values():
            parents.difference_update(ready)

    # Fail before any DROP for objects whose behavior we cannot safely restore.
    unsupported = connection.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_trigger
          WHERE tgrelid=ANY(CAST(:ids AS oid[])) AND NOT tgisinternal)
        OR EXISTS(SELECT 1 FROM pg_rewrite
          WHERE ev_class=ANY(CAST(:ids AS oid[])) AND rulename<>'_RETURN')
    """), {"ids": ids})
    if unsupported:
        raise RuntimeError("Profile-backed views have custom triggers/rules; explicit migration required.")
    for oid, view in views.items():
        view["indexes"] = list(connection.execute(text("""
            SELECT pg_get_indexdef(indexrelid),c.relname,obj_description(c.oid,'pg_class'),
                   i.indisclustered
            FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            WHERE indrelid=:oid ORDER BY c.relname
        """), {"oid": oid}).all())
        view["columns"] = list(connection.execute(text("""
            SELECT attname,col_description(attrelid,attnum)
            FROM pg_attribute WHERE attrelid=:oid AND attnum>0 AND NOT attisdropped
            ORDER BY attnum
        """), {"oid": oid}).all())
        view["grants"] = list(connection.execute(text("""
            SELECT CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
                   x.privilege_type,x.is_grantable
            FROM pg_class c CROSS JOIN LATERAL aclexplode(
              COALESCE(c.relacl,acldefault('r',c.relowner))) x WHERE c.oid=:oid
        """), {"oid": oid}).all())
        view["column_grants"] = list(connection.execute(text("""
            SELECT a.attname,
              CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
              x.privilege_type,x.is_grantable
            FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
            WHERE a.attrelid=:oid AND a.attnum>0 AND NOT a.attisdropped
        """), {"oid": oid}).all())

    def sql_literal(value):
        # Values come from PostgreSQL metadata, never interpolate them raw.
        return connection.scalar(text("SELECT quote_literal(:value)"), {"value": value})

    for oid in reversed(ordered):
        view = views[oid]
        kind = "MATERIALIZED VIEW" if view["relkind"] == "m" else "VIEW"
        connection.exec_driver_sql(
            f"DROP {kind} {qualified(view['nspname'],view['relname'])}")
    for oid in ordered:
        view = views[oid]
        name = qualified(view["nspname"], view["relname"])
        materialized = view["relkind"] == "m"
        kind = "MATERIALIZED VIEW" if materialized else "VIEW"
        definition = view["definition"].strip().removesuffix(";")
        if view["relname"] == "active_profiles" and view["nspname"] == connection.scalar(
                text("SELECT current_schema()")):
            definition = (f"SELECT * FROM {qualified(view['nspname'],'profiles')} "
                          "WHERE merged_into_id IS NULL AND NOT is_deleted")
        options = ""
        if view["reloptions"]:
            entries = []
            for setting in view["reloptions"]:
                key, value = setting.split("=", 1)
                entries.append(f"{quote(key)}={sql_literal(value)}")
            options = " WITH (" + ",".join(entries) + ")"
        method = f" USING {quote(view['access_method'])}" if materialized and view["access_method"] else ""
        tablespace = f" TABLESPACE {quote(view['tablespace'])}" if materialized and view["tablespace"] else ""
        data = (" WITH DATA" if view["relispopulated"] else " WITH NO DATA") if materialized else ""
        connection.exec_driver_sql(
            f"CREATE {kind} {name}{method}{options}{tablespace} AS {definition}{data}")
        for index_sql, index_name, comment, clustered in view["indexes"]:
            connection.exec_driver_sql(index_sql)
            if comment is not None:
                connection.exec_driver_sql(
                    f"COMMENT ON INDEX {qualified(view['nspname'],index_name)} IS {sql_literal(comment)}")
            if clustered:
                connection.exec_driver_sql(f"ALTER MATERIALIZED VIEW {name} CLUSTER ON {quote(index_name)}")
        if view["comment"] is not None:
            connection.exec_driver_sql(f"COMMENT ON {kind} {name} IS {sql_literal(view['comment'])}")
        for column, comment in view["columns"]:
            if comment is not None:
                connection.exec_driver_sql(
                    f"COMMENT ON COLUMN {name}.{quote(column)} IS {sql_literal(comment)}")
        # Remove grants introduced by current default privileges, then restore
        # the original relation/column ACL (including PUBLIC and grant options).
        current_grantees = connection.execute(text("""
            SELECT DISTINCT CASE WHEN x.grantee=0 THEN 'PUBLIC'
              ELSE pg_get_userbyid(x.grantee) END
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) x
            WHERE n.nspname=:schema AND c.relname=:name
        """), {"schema": view["nspname"], "name": view["relname"]}).scalars().all()
        role_sql = lambda role: "PUBLIC" if role == "PUBLIC" else quote(role)
        for role in current_grantees:
            connection.exec_driver_sql(f"REVOKE ALL ON {name} FROM {role_sql(role)}")
        for role, privilege, grantable in view["grants"]:
            connection.exec_driver_sql(
                f"GRANT {privilege} ON {name} TO {role_sql(role)}"
                + (" WITH GRANT OPTION" if grantable else ""))
        for column, role, privilege, grantable in view["column_grants"]:
            connection.exec_driver_sql(
                f"GRANT {privilege} ({quote(column)}) ON {name} TO {role_sql(role)}"
                + (" WITH GRANT OPTION" if grantable else ""))
        connection.exec_driver_sql(f"ALTER {kind} {name} OWNER TO {quote(view['owner'])}")
    if relation_columns(connection, "active_profiles") != relation_columns(connection, "profiles"):
        raise RuntimeError("active_profiles column list still differs from profiles; rolling back.")
    connection.execute(text("SELECT set_config('lock_timeout',:previous,true)"),
                       {"previous": previous_timeout})


def version_apply_guard(connection):
    """Alembic callback with a before/after base-column signature."""
    previous = profile_signature(connection)

    def after_revision(ctx, **kwargs):
        nonlocal previous
        current = profile_signature(connection)
        active = relation_columns(connection, "active_profiles")
        if (current != previous or (
                active and active != relation_columns(connection, "profiles"))):
            refresh_profile_views(connection)
        if relation_columns(connection, "profiles") != relation_columns(connection, "active_profiles"):
            raise RuntimeError("Migration left active_profiles incompatible with profiles.")
        previous = current
    return after_revision
