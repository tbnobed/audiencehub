"""Real SQL DSL acceptance against the isolated benchmark database."""
from datetime import date
import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from test_consent_ingestion_pg import db
from app.segments.compiler import compile_definition, validate_definition, SegmentValidationError
from app.segments.registry import field_registry
from app.segments.templates import template_definitions

PIN = date(2024, 12, 31)


def fixture(db):
    db.execute(text("INSERT INTO sources(id,key,name,kind) OVERRIDING SYSTEM VALUE VALUES(1,'crm','CRM','csv')"))
    db.execute(text("""INSERT INTO profiles(id,first_name,region,first_seen_at)
        OVERRIDING SYSTEM VALUE VALUES(1,'A%_','TX','2024-12-31'),(2,'Bob','OK','2024-12-01'),(3,NULL,NULL,'2020-01-01')"""))
    db.execute(text("""INSERT INTO profile_traits(profile_id,ltv_total,gift_amount_12m,donor_status,is_recurring_active,source_keys,last_gift_date)
        VALUES(1,100,100,'active',true,ARRAY['crm'],'2024-12-31'),
        (2,50,50,'lapsed',false,ARRAY['crm'],'2024-12-01')"""))
    db.execute(text("""INSERT INTO gifts(source_id,external_id,profile_id,amount,gift_date,fund,is_recurring)
        VALUES(1,'g1',1,10,'2024-12-31','Mission%_',true),(1,'g2',1,20,'2024-12-01','Other',false),
        (1,'g3',2,5,'2023-01-01','Other',false)"""))
    db.execute(text("INSERT INTO consents(profile_id,channel,status,source_id) VALUES(1,'email','opted_in',1),(2,'email','opted_out',1)"))
    db.execute(text("""INSERT INTO enrichment_attributes(source_id,key,label,data_type) VALUES(1,'score','Score','number');
        INSERT INTO enrichment_values(profile_id,source_id,attribute_key,value_num,license_expires_at)
        VALUES(1,1,'score',9,'2024-12-31'),(2,1,'score',10,'2024-12-30')"""))


def rows(db, rule):
    definition = {"version": 1, "root": {"op": "and", "rules": [rule]}}
    return sorted(db.scalars(compile_definition(db, definition, PIN)))


@pytest.mark.parametrize("kind,field,op,value,expected", [
    ("profile","first_name","eq","Bob",[2]), ("profile","first_name","neq","Bob",[1]),
    ("profile","first_name","in",["Bob"],[2]), ("profile","first_name","not_in",["Bob"],[1]),
    ("profile","first_name","contains","%_",[1]), ("profile","first_name","starts_with","A%",[1]),
    ("profile","first_name","is_null",None,[3]), ("profile","first_name","is_not_null",None,[1,2]),
    ("trait","ltv_total","eq",50,[2]), ("trait","ltv_total","neq",50,[1]),
    ("trait","ltv_total","gt",50,[1]), ("trait","ltv_total","gte",50,[1,2]),
    ("trait","ltv_total","lt",100,[2]), ("trait","ltv_total","lte",100,[1,2]),
    ("trait","ltv_total","between",[50,99],[2]), ("trait","ltv_total","is_null",None,[3]),
    ("trait","ltv_total","is_not_null",None,[1,2]),
    ("trait","last_gift_date","before","2024-12-31",[2]),
    ("trait","last_gift_date","after","2024-12-01",[1]),
    ("trait","last_gift_date","between",["2024-12-01","2024-12-31"],[1,2]),
    ("trait","last_gift_date","in_last_days",30,[1]),
    ("trait","last_gift_date","not_in_last_days",30,[2]),
    ("trait","last_gift_date","is_null",None,[3]), ("trait","last_gift_date","is_not_null",None,[1,2]),
    ("profile","first_seen_at","before","2024-12-01",[3]),
    ("profile","first_seen_at","after","2024-12-01",[1]),
    ("profile","first_seen_at","between",["2024-12-01","2024-12-31"],[1,2]),
    ("profile","first_seen_at","in_last_days",30,[1]),
    ("profile","first_seen_at","not_in_last_days",30,[2,3]),
    ("profile","first_seen_at","is_null",None,[]),
    ("profile","first_seen_at","is_not_null",None,[1,2,3]),
    ("trait","is_recurring_active","is_true",None,[1]),
    ("trait","is_recurring_active","is_false",None,[2]),
    ("trait","donor_status","in",["active"],[1]),
    ("trait","donor_status","not_in",["active"],[2]),
])
def test_segments_engine_scalar_operators(db, kind, field, op, value, expected):
    fixture(db)
    assert rows(db, dict(type=kind, field=field, operator=op, value=value)) == expected


def test_segments_engine_aggregates_permissions_enrichment_sources(db):
    fixture(db)
    assert rows(db, dict(type="gift", aggregate="count", operator="eq", value=0)) == [3]
    assert rows(db, dict(type="gift", aggregate="sum", operator="eq", value=30)) == [1]
    assert rows(db, dict(type="gift", aggregate="sum", operator="eq", value=0)) == [3]
    assert rows(db, dict(type="gift", aggregate="max", operator="eq", value=20)) == [1]
    assert rows(db, dict(type="gift", aggregate="min", operator="is_null")) == [3]
    assert rows(db, dict(type="gift", aggregate="exists", operator="is_false")) == [3]
    rule = dict(type="gift", aggregate="count", operator="eq", value=1,
                window={"kind":"last_days","days":30}, where={"fund":{"operator":"contains","value":"%_"}})
    assert rows(db, rule) == [1]
    assert rows(db, dict(type="consent", channel="email", status="unknown")) == [3]
    assert rows(db, dict(type="enrichment", source="crm", field="score", operator="gte", value=9)) == [1]
    assert rows(db, dict(type="enrichment", source="crm", field="score", operator="is_null")) == [2,3]
    assert rows(db, dict(type="source", source_key="crm", operator="not_in")) == [3]


def test_segments_engine_events_nested_negation_and_injection(db):
    fixture(db)
    db.execute(text("""INSERT INTO events(profile_id,source_id,type,name,occurred_at,properties)
        VALUES(1,1,'track','Video Watched','2024-12-31 23:59:59+00','{"topic":"a%_"}'),
        (2,1,'track','Video Watched','2024-12-01 23:59:59+00','{"topic":"a%_"}')"""))
    rule = dict(type="event", name="Video Watched", aggregate="count", operator="gte", value=1,
                where={"properties.topic":{"operator":"eq","value":"a%_"}}, window={"kind":"last_days","days":30})
    assert rows(db, rule) == [1]
    assert rows(db, {"op":"or","negate":True,"rules":[rule,dict(type="profile",field="id",operator="eq",value=2)]}) == [3]
    injection = "'; DROP TABLE profiles; --"
    definition = {"version":1,"root":{"type":"profile","field":"first_name","operator":"eq","value":injection}}
    compiled = compile_definition(db,definition,PIN).compile(dialect=postgresql.dialect())
    assert injection not in str(compiled) and injection in compiled.params.values()
    assert list(db.scalars(compile_definition(db,definition,PIN))) == []


def test_segments_engine_top_five_ties_single_and_zero(db):
    fixture(db)
    rule = dict(type="trait",field="top_5_percent_12m",operator="is_true")
    assert rows(db,rule) == [1]
    db.execute(text("UPDATE profile_traits SET gift_amount_12m=100"))
    assert rows(db,rule) == [1,2]
    db.execute(text("UPDATE profile_traits SET gift_amount_12m=0 WHERE profile_id=2"))
    assert rows(db,rule) == [1]
    db.execute(text("UPDATE profile_traits SET gift_amount_12m=0"))
    assert rows(db,rule) == []


@pytest.mark.parametrize("rule,path", [
    (dict(type="profile",field="secret",operator="eq",value="x"),".field"),
    (dict(type="trait",field="ltv_total",operator="contains",value=2),".operator"),
    (dict(type="trait",field="donor_status",operator="in",value=["wrong"]),".value[0]"),
    (dict(type="profile",field="id",operator="eq",value=True),".value"),
    (dict(type="profile",field="first_seen_at",operator="between",value=["2024-02-30","2024-12-31"]),".value[0]"),
])
def test_segments_engine_path_errors(db,rule,path):
    with pytest.raises(SegmentValidationError) as exc:
        rows(db,rule)
    assert exc.value.errors[0]["path"] == "root.rules[0]" + path


def test_segments_engine_limits_templates_and_registry(db):
    fixture(db)
    rule = {"type":"profile","field":"id","operator":"gte","value":1}
    with pytest.raises(SegmentValidationError):
        rows(db,{"op":"and","rules":[rule]*51})
    nested = rule
    for _ in range(6):
        nested = {"op":"and","rules":[nested]}
    with pytest.raises(SegmentValidationError):
        rows(db,nested)
    templates = template_definitions()
    assert len(templates) == 6
    for template in templates:
        assert "donor" not in template["name"].lower()
        validate_definition(db,template["definition"],PIN)
        list(db.scalars(compile_definition(db,template["definition"],PIN)))
    assert any(f["source"] == "crm" for f in field_registry(db)["fields"] if f["type"]=="enrichment")


def test_segments_engine_materialized_reference(db):
    fixture(db)
    sid = db.scalar(text("INSERT INTO segments(name,status) VALUES('Audience','active') RETURNING id"))
    db.execute(text("INSERT INTO segment_membership(segment_id,profile_id) VALUES(:s,1)"),{"s":sid})
    assert rows(db,dict(type="segment",segment_id=sid,operator="in")) == [1]
    assert rows(db,dict(type="segment",segment_id=sid,operator="not_in")) == [2,3]
    db.execute(text("UPDATE segments SET status='archived'"))
    with pytest.raises(SegmentValidationError):
        rows(db,dict(type="segment",segment_id=sid,operator="in"))


def test_segments_engine_timestamp_instants_and_malformed_type(db):
    fixture(db)
    assert rows(db,dict(type="profile",field="first_seen_at",operator="after",value="2024-12-01T00:00:00Z")) == [1]
    assert rows(db,dict(type="profile",field="first_seen_at",operator="between",
                       value=["2024-12-01", "2024-12-31T00:00:00Z"])) == [1,2]
    with pytest.raises(SegmentValidationError):
        rows(db, {"type": [], "operator": "eq", "value": 1})