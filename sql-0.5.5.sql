-- Fresh AI Engine 0.5.5 fallback migration.
-- Spusť pouze v databázi Enginu, pokud automatický schema upgrade nemá právo ALTER.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_data_payload LONGTEXT NULL AFTER context_payload;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS output_contract_payload LONGTEXT NULL AFTER input_data_payload;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result_type VARCHAR(20) NULL AFTER response_payload;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result_schema_id VARCHAR(160) NULL AFTER result_type;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result_validated TINYINT(1) NOT NULL DEFAULT 0 AFTER result_schema_id;
