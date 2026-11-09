# Nasazení Fresh AI Engine 0.5.9

## Co opravuje

0.5.9 je reliability hotfix nad 0.5.8. Opravuje dvě konkrétní produkční chyby:

1. Passenger/cPanel mohl při `send_file(BytesIO(...))` shodit diagnostický ZIP na `io.UnsupportedOperation: fileno`. ZIP se nyní posílá jako běžná byte response.
2. Router 2.0 může vytvořit diagnostický JSON větší než 64 KiB. MySQL `TEXT` jej mohl oříznout a následné `json.loads()` skončilo `JSONDecodeError`. `routing_decisions.trace_json` a velké transientní payloady jsou proto LONGTEXT a čtení starých poškozených trace je fail-safe.

## Postup

1. Zálohuj databázi a současnou aplikaci.
2. Zachovej produkční `.env` a hostingové `.htaccess`.
3. Nahraj celý obsah 0.5.9 do skutečného Passenger Application Root.
4. Restartuj Passenger.
5. Ověř `/v1/health` → `0.5.9-cpanel-wsgi`.
6. V adminu otevři Manifest, klikni **Obnovit odkaz** a potom **Stáhnout ZIP**.
7. Spusť jeden Boban `seo.metadata.optimize` request a ověř, že nevznikne anonymní HTTP 500.

## Migrace

Migrace se spouští automaticky při startu. Pokud DB účet nemá `ALTER`, po záloze spusť `SQL-0.5.9.sql` ručně.

Stávající již oříznutý `trace_json` se zpětně neopravuje; aplikace jej bezpečně ignoruje. Nové trace se uloží celé.
