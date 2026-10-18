"""SFTP activation selections; additive disabled reference configurations."""
from alembic import op

revision = "0034_activation_sftp"
down_revision = "0033_source_pulls"
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("""
      ALTER TABLE activations ADD COLUMN attribute_mode text NOT NULL DEFAULT 'selected'
        CHECK (attribute_mode IN ('selected','all'));
      INSERT INTO destinations(name,type,config,is_active)
      SELECT n,'sftp',jsonb_build_object('output_dir','/exports','gzip',false,'include_pii',true),false
      FROM (VALUES ('CustomerPulse'),('Trilogy')) v(n)
      WHERE NOT EXISTS(SELECT 1 FROM destinations d WHERE d.name=v.n);
      INSERT INTO activations(name,segment_id,destination_id,field_mapping,column_order,
        attribute_mode,schedule,timezone,is_active)
      SELECT v.n,s.id,d.id,'{}'::jsonb,'{}'::text[],'all',v.cron,'America/New_York',false
      FROM (VALUES
        ('CustomerPulse – All TBN Reference','CustomerPulse','All TBN Reference','0 0 2 * *'),
        ('CustomerPulse – RFM Donor Reference','CustomerPulse','RFM TBN All Donor Reference','0 0 1 * *'),
        ('Trilogy – RFM Donor Reference','Trilogy','RFM TBN All Donor Reference','0 0 29 * *')
      ) v(n,dest,seg,cron)
      JOIN LATERAL (SELECT id FROM segments WHERE name=v.seg ORDER BY id LIMIT 1) s ON true
      JOIN LATERAL (SELECT id FROM destinations WHERE name=v.dest AND type='sftp' ORDER BY id LIMIT 1) d ON true
      WHERE NOT EXISTS(SELECT 1 FROM activations a WHERE a.name=v.n);
      INSERT INTO destinations(name,type,config,is_active)
      SELECT 'Identity Crosswalk','sftp',
        jsonb_build_object('output_dir','/exports','gzip',false,'export_kind','crosswalk','include_pii',true),false
      WHERE NOT EXISTS(SELECT 1 FROM destinations WHERE name='Identity Crosswalk');
      INSERT INTO activations(name,segment_id,destination_id,field_mapping,column_order,
        attribute_mode,schedule,timezone,is_active,required_consent)
      SELECT 'Identity Crosswalk – Daily',s.id,d.id,'{}'::jsonb,'{}'::text[],
        'selected','0 0 * * *','America/New_York',false,'email'
      FROM (SELECT id FROM segments WHERE name='All TBN Reference' ORDER BY id LIMIT 1) s,
        (SELECT id FROM destinations WHERE name='Identity Crosswalk' AND type='sftp' ORDER BY id LIMIT 1) d
      WHERE NOT EXISTS(SELECT 1 FROM activations WHERE name='Identity Crosswalk – Daily');
    """)


def downgrade():
    op.execute("ALTER TABLE activations DROP COLUMN attribute_mode")
