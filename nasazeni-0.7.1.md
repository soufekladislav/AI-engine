# Fresh AI Engine 0.7.1 — Fresh Key Propojení

Tato verze opravuje význam Fresh Key projektů. AI Engine už v uživatelském rozhraní nezakládá další pracovní projekty. Fresh Key pouze eviduje odkazy na projekty existující v jednotlivých aplikacích a spojuje je pod jeden společný Fresh Key ID.

## Co se mění

- stránka Fresh Key se uživatelsky jmenuje **Propojení**;
- zmizel formulář „Přidat projekt“ z AI Enginu;
- aplikace mohou nejdřív pouze nahlásit svůj existující lokální projekt do Fresh Key inventáře;
- uživatel může několik lokálních projektů z různých aplikací spojit pod jeden Fresh Key ID;
- k existujícímu propojení lze později přidat další lokální projekt;
- původní `/v1/fresh-key/projects/*` endpointy zůstávají jako kompatibilní aliasy;
- nové preferované endpointy jsou `/v1/fresh-key/local-projects/*` a `/v1/fresh-key/links/*`;
- pracovní data se nikam nekopírují.

## Databáze

0.7.1 přidává pouze dvě inventární tabulky:

- `fresh_key_local_projects`
- `fresh_key_local_project_access`

Při běžném startu je vytvoří `Base.metadata.create_all()`. Pokud databázový účet nemá CREATE oprávnění, spusť `sql-0.7.1-fresh-key-links.sql` ručně.

## Nasazení

1. Zálohuj aktuální adresář aplikace a databázi.
2. Zachovej produkční `.env`; ZIP ho neobsahuje a nesmíš ho přepsat.
3. Nahraj soubory 0.7.1 přes stávající aplikaci.
4. Restartuj Passenger/cPanel Python aplikaci (`touch tmp/restart.txt` je možné použít, pokud ho hosting respektuje).
5. Ověř diagnostiku: `engine_version` musí být `0.7.1`.
6. Přihlas se do zákaznického účtu a otevři **Propojení**.

## Důležitá poznámka k migraci

Stávající tabulky `fresh_key_projects` a `fresh_key_project_bindings` se nemažou ani nepřejmenovávají. V 0.7.1 fungují interně jako společný Fresh Key ID a jeho vazby. Změna je záměrně kompatibilní se 0.7.0 a nevyžaduje migraci stávajících OAuth připojení.
