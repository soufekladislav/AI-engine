-- Fresh AI Engine 0.5.9 fallback migration (MySQL/MariaDB)
-- Spouštěj pouze po záloze a jen pokud automatická migrace nemá ALTER oprávnění.
ALTER TABLE routing_decisions MODIFY trace_json LONGTEXT NOT NULL;
ALTER TABLE jobs
  MODIFY prompt_payload LONGTEXT NULL,
  MODIFY context_payload LONGTEXT NULL,
  MODIFY response_payload LONGTEXT NULL;
