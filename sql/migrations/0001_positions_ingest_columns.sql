-- OPTIONAL catch-up for databases created from the pre-PR2 schema.sql
-- (developer volumes / leftover test DBs). No-op on a fresh PR2 baseline.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
UPDATE positions SET observed_at = ts WHERE observed_at IS NULL;  -- fetch time unknown; ts is the closest bound
ALTER TABLE positions ALTER COLUMN observed_at SET DEFAULT NOW();
ALTER TABLE positions ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS snapshot_key TEXT;
