-- Fresh AI Engine 0.5.4 fallback migration.
-- Spusť pouze v databázi Enginu, pokud automatický schema upgrade nemá právo ALTER.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS operation VARCHAR(160) NULL AFTER task_type;
