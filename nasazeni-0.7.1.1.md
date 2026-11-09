# Fresh AI Engine 0.7.1.1 — startovací hotfix Fresh Key

Opravuje HTTP 500 při startu 0.7.1 způsobené tím, že nové `/links/*` routy a legacy `/projects/*` aliasy používaly stejné Flask endpoint názvy.

## Nasazení

1. Zachovej produkční `.env` beze změny.
2. Nahraj soubory balíčku přes 0.7.1. Minimální hotfix je `app/fresh_key.py`; doporučen je celý balíček 0.7.1.1.
3. Není potřeba žádná SQL migrace oproti 0.7.1.
4. Restartuj Passenger (`touch tmp/restart.txt` nebo Restart v cPanelu).
5. Ověř `/health` / diagnostiku a zákaznickou stránku Fresh Key.

## Co se změnilo

- kompatibilní `/projects/*` aliasy mají vlastní unikátní Flask endpoint názvy;
- registrace Fresh Key Blueprintu je idempotentní a při opakovaném volání nepřidává routy podruhé;
- zachován model Propojení a všechny DB tabulky 0.7.1.
