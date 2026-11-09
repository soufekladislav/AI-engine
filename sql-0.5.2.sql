-- MANUÁLNÍ ZÁLOŽNÍ MIGRACE; spustit jen pokud automatické rozšíření nenastalo.
-- Nejprve zkontroluj v phpMyAdmin strukturu tabulky job_billing.
ALTER TABLE job_billing
 ADD COLUMN credit_unit_price_czk DOUBLE NULL,
 ADD COLUMN usd_czk_rate DOUBLE NULL,
 ADD COLUMN input_price_usd_per_million DOUBLE NULL,
 ADD COLUMN output_price_usd_per_million DOUBLE NULL;
