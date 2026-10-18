"""Add reference deliveries without editing any existing user configuration."""
from sqlalchemy import select
from app.models import Activation, Destination, Segment

REFERENCES = [
    ("CustomerPulse – All TBN Reference", "CustomerPulse", "All TBN Reference", "0 0 2 * *", "audience"),
    ("CustomerPulse – RFM Donor Reference", "CustomerPulse", "RFM TBN All Donor Reference", "0 0 1 * *", "audience"),
    ("Trilogy – RFM Donor Reference", "Trilogy", "RFM TBN All Donor Reference", "0 0 29 * *", "audience"),
    ("Identity Crosswalk – Daily", "Identity Crosswalk", "All TBN Reference", "0 0 * * *", "crosswalk"),
]


def bootstrap(db):
    from sqlalchemy import text
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('activation-reference-bootstrap'))"))
    created, missing = [], []
    for name, dest_name, segment_name, cron, kind in REFERENCES:
        if db.scalar(select(Activation.id).where(Activation.name == name).limit(1)):
            continue
        segment = db.scalar(select(Segment).where(Segment.name == segment_name).order_by(Segment.id).limit(1))
        if not segment:
            missing.append(segment_name)
            continue
        dest = db.scalar(select(Destination).where(Destination.name == dest_name).order_by(Destination.id).limit(1))
        if dest and dest.type != "sftp":
            missing.append(dest_name + ": existing destination is not SFTP")
            continue
        if not dest:
            dest = Destination(name=dest_name, type="sftp", is_active=False,
                               config={"output_dir": "/exports", "gzip": False, "export_kind": kind})
            db.add(dest)
            db.flush()
        row = Activation(name=name, segment_id=segment.id, destination_id=dest.id,
                         field_mapping={}, column_order=[], attribute_mode="all" if kind == "audience" else "selected",
                         schedule=cron, timezone="America/New_York", is_active=False,
                         required_consent="email" if kind == "crosswalk" else None)
        db.add(row)
        db.flush()
        created.append(row.id)
    return {"created": created, "unavailable": sorted(set(missing))}
