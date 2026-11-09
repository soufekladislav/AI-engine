-- Fresh AI Engine 0.5.7 fallback migration (MariaDB/MySQL)
-- POUZE pokud automatický upgrade při startu neproběhne.
-- Před každým ALTER TABLE ověř, zda sloupec již neexistuje.

-- 0.5.5 job contract columns (spusť jen chybějící):
-- ALTER TABLE jobs ADD COLUMN input_data_payload LONGTEXT NULL;
-- ALTER TABLE jobs ADD COLUMN output_contract_payload LONGTEXT NULL;
-- ALTER TABLE jobs ADD COLUMN result_type VARCHAR(20) NULL;
-- ALTER TABLE jobs ADD COLUMN result_schema_id VARCHAR(160) NULL;
-- ALTER TABLE jobs ADD COLUMN result_validated TINYINT(1) NOT NULL DEFAULT 0;

-- 0.5.6 Router profile (spusť jen pokud chybí):
-- ALTER TABLE model_routes ADD COLUMN routing_profile_json LONGTEXT NULL;

CREATE TABLE IF NOT EXISTS job_feedback (
  job_id VARCHAR(36) NOT NULL,
  outcome VARCHAR(20) NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (job_id),
  KEY ix_job_feedback_outcome (outcome),
  KEY ix_job_feedback_created_at (created_at),
  CONSTRAINT fk_job_feedback_job
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
