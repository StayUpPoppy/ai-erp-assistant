-- Allow different ERP users to upload the same source file independently.
-- SQLAlchemy model now treats file_hash as a normal indexed column; ownership
-- isolation is enforced by user_id + file_hash in application logic.

ALTER TABLE ingestions DROP CONSTRAINT IF EXISTS ingestions_file_hash_key;

CREATE INDEX IF NOT EXISTS ix_ingestions_file_hash ON ingestions (file_hash);
CREATE INDEX IF NOT EXISTS ix_ingestions_user_id_file_hash ON ingestions (user_id, file_hash);
