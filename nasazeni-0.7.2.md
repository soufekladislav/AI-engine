# Fresh AI Engine 0.7.2 — nasazení

## Co se mění
- Fresh Key používá jednotný název **Propojení**.
- Přehled je řádkový: projekt + připojené aplikace; po rozbalení uživatelé, aplikace a konektory.
- Uživatelé a role se spravují centrálně a stejný seznam čtou Linda, Boban, Eda a další aplikace.
- Ověřený Fresh AI účet se při sdílení přidá ihned. Neznámý/neověřený e-mail vytvoří čekající členství a odešle pozvánku.
- Role Správce automaticky získává právo spravovat Propojení.
- Není potřeba nová SQL migrace proti 0.7.1.1.

## Nasazení
1. Zálohuj současnou aplikaci a databázi.
2. Zachovej produkční `.env` — balíček ho neobsahuje.
3. Nahraj soubory 0.7.2 přes 0.7.1.1.
4. Zachovej hlavně `FRESH_ACCOUNT_*`, `FRESH_KEY_*`, databázové údaje a Engine/API klíče.
5. Restartuj Passenger (`touch tmp/restart.txt`).
6. Ověř admin login, zákaznický login a `/account/fresh-key`.

## Poznámka k oprávněním
Fresh Key je zdroj pravdy pro sdílené členství. Pracovní oprávnění/licence uvnitř jednotlivých aplikací zůstávají jejich vlastní odpovědností.
