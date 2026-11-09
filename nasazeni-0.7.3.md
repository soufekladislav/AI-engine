# Fresh AI Engine 0.7.3 – Fresh Key potvrzované Propojení

## Princip

Fresh Key **nevlastní pracovní projekty**. Pouze eviduje, že lokální projekt v Lindě, Bobanovi, Edovi, Karlovi nebo jiné Fresh aplikaci patří ke stejnému společnému Propojení.

- zdrojová aplikace odešle existující projekt do Fresh Key explicitně tlačítkem **Odeslat do Fresh Key**;
- v AI Enginu se zobrazí jeden řádek Propojení s připojenými aplikacemi;
- tlačítko **Připojit další App** nabídne existující projekty cílové aplikace nebo návrh na vytvoření nového;
- návrh zůstává `pending` a nic se nespojí ani nevytvoří bez potvrzení přímo v cílové aplikaci;
- cílová aplikace zobrazí upozornění a Fresh Key odešle e-mail s odkazem k potvrzení;
- názvy lokálních projektů se mohou lišit – uživatel rozhoduje, které projekty patří k sobě.

## Lidé a role

AI Engine zobrazuje uživatele u Propojení pouze jako **přehled**. Přidání člověka, odebrání a lokální role se spravují vždy v konkrétní aplikaci. Legacy formulář pro editaci členů v AI Enginu je v 0.7.3 záměrně read-only/odmítnutý, aby centrální vrstva nemohla obejít lokální oprávnění aplikace.

Aplikace mohou pro e-mailovou komunikaci použít `POST /v1/fresh-key/project-share/notify`. Endpoint pouze rozešle potvrzení spolupráce / pozvánku a **neuděluje lokální přístup**. Přístup vytváří a spravuje zdrojová aplikace.

## Databáze

Nová tabulka `fresh_key_link_requests` drží návrhy propojení (`pending`, `accepted`, `declined`, `expired`). Tabulka `fresh_key_project_app_members` je pouze informační zrcadlo lokálního členství po aplikacích, aby AI Engine uměl správně ukázat např. „Tomáš: Linda · Editor, Boban · Čtenář“ bez přebírání lokálních oprávnění. Při běžném startu obě vytvoří `Base.metadata.create_all()`. Pokud DB uživatel nemá `CREATE TABLE`, spusť ručně:

`sql-0.7.3-fresh-key-link-requests.sql`

## Nasazení

1. Zálohuj aplikaci a databázi.
2. **Zachovej produkční `.env`**. Balíček obsahuje pouze `.env.example`.
3. Přepiš aplikační soubory verzí 0.7.3.
4. Ověř/vytvoř tabulky `fresh_key_link_requests` a `fresh_key_project_app_members`.
5. Restartuj Passenger: `touch tmp/restart.txt`.
6. Ověř `/admin/diagnostics`, zákaznický login a `/account/fresh-key`.

## Volitelné proměnné

`FRESH_KEY_LINDA_URL`, `FRESH_KEY_BOBAN_URL`, `FRESH_KEY_EDA_URL`, `FRESH_KEY_KAREL_URL` určují odkazy v potvrzovacích e-mailech. Mají produkční výchozí hodnoty.

## Kompatibilita

Existující `fresh_key_projects`, lokální inventář, OAuth konektory, členství a vazby zůstávají zachované. Staré endpointy zůstávají kvůli kompatibilitě, ale 0.7.3 používá workflow **návrh → potvrzení v cílové aplikaci → propojení**.
