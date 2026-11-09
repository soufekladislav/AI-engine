# Fresh AI Engine 0.5.0 + Fresh AI Chat 0.3.0

Tento postup platí pro **existující dvě oddělené produkční instalace na cPanelu**. Aktualizace probíhá bez vymazání zákazníků, objednávek, konverzací, personalizace nebo databázových přístupů. **ZIPy neobsahují produkční `.env`, databáze, logy ani cPanel `.htaccess`.** Samotné ZIPy nebyly spuštěny na produkci; úspěch ověř po nasazení.

## Co je implementované

- Fresh AI Chat už neposílá všechny požadavky jako `task_type=chat`. Odesílá poslední relevantní konverzační výměny jako zprávy a svou osobnost / systémový tón; Engine samostatně přiřadí schopnost a volí poskytovatele. U navazujících krátkých zadání umí Engine převzít typ předchozí úlohy.
- Textový routing v Enginu: kontrola schopnosti → minimální kvalita → odhad nákladů včetně kontextu → záložní trasy. Režim `high` vybírá nejvyšší dostupnou kvalitu (u shodných kvalit podle ceny). Neznamená to náhodné střídání modelů: pokud je OpenAI podle **aktuálně uložených cen** nejlevnější způsobilá trasa, zůstane zvolený OpenAI. Konkrétní modelové API identifikátory a ceny jsou ponechané ve stávající DB, neověřovali jsme jejich současnou nabídku ani jejich API z produkce.
- Chat udržuje celou historii ve své vlastní databázi. Nová tabulka `chat_memory` ukládá pouze **výslovně zadané** poznámky „Zapamatuj si: …“ oddělené podle `chat_users.id`; uživatel má na `/account/memory` možnost své poznámky zobrazit a smazat. Při dotazu Engine dostává jen tematicky vybrané poznámky daného uživatele, nikoliv globální kopii chatu; pokud se uživatel ptá, co má uložené k určitému tématu, může Engine poznámku **doslova citovat** bez placené AI. Bez nalezené podpory nevymýšlí odpověď a vybere model. Staré rozhovory se automaticky **nepřevádějí** na „ověřené znalosti“.
- Nové lokální vyřízení z paměti není původní testovací provider `mock-local` (ten zůstává pouze pro ruční testy). Lokální cesta vyžaduje **samostatný tajný klíč pro Chat**; jiná aplikace ani běžný API klíč nesmějí podvrhnout soukromé poznámky pro lokální odpověď.
- Chat odpověď z Enginu ukládá a zobrazuje. Při generování textu Engine předává zvolenému poskytovateli i komunikační styl uživatele. Chat **nevolá další model kvůli každému přepisování tónu**. U lokálních odpovědí jde zatím o předem formulovanou českou větu/citaci.

## Limity této verze (nepřisuzovat jí neexistující funkce)

**Není to zatím plná sémantická paměť napříč každou větou všech chatů ani sdílení dat všech projektů a aplikací.** Křížové vyhledávání se nyní týká uživatelových výslovně uložených poznámek; běžné zprávy zůstávají v úplné původní historii pouze svého vlastníka. Sdílené projektové ACL, automatické extrakce potvrzených rozhodnutí, konektory jiných aplikací a vyhledávání nad jejich daty se budou řešit samostatně. Odstranění původního chatu odstraní i jeho výslovně uložené poznámky (vazba na zdrojovou konverzaci).

Obrázky lze řešit samostatným stávajícím media API Enginu, ale Chat 0.3.0 ještě nemá kompletní obrazové/video uživatelské rozhraní, správu souborů, dokončovací webhooky ani účtování těchto médií. Video adaptér není dle zaslaného manifestu zprovozněn; nezapínej `video_generation` jen změnou seznamu schopností.

### 1) Před aktualizací

1. V cPanelu zazálohuj **obě samostatné aplikační složky a obě produkční databáze**. Zálohuj zvlášť stávající `.env`, serverem generované `.htaccess`, `passenger_wsgi.py` a případné soubory mimo ZIP. Nepřepisuj produkční `.env` ukázkovou verzí z balíčku.
2. Ověř, že Engine běží na vlastní doméně `aiengine.freshcrackers.cz`, Chat na `chat.freshcrackers.cz`. Oba mají samostatnou databázi a v cPanelu startup file `passenger_wsgi.py`, entrypoint `application`, Python 3.11. U Enginu měl zaslaný archiv **chybně namíchané soubory Chatu v kořeni a Passenger vstupní bod importoval Chat**; nový engine balíček to odděluje.
3. V současné databázi **Chatu** (nikoliv Enginu) spusť `fresh-ai-chat-0.3.0-upgrade.sql`. Je to pouze `CREATE TABLE IF NOT EXISTS chat_memory`; původní data se nemažou.
4. V současné databázi **Enginu** (nikoliv Chatu) spusť `fresh-ai-engine-0.5.0-upgrade.sql`. Pouze rozšíří capabilities stávající trasy `anthropic-quality` o `chat,general`, pokud chybí; zachová modelové ID, ceny, aktivaci, kredity a ostatní trasy. Neimportuj znovu kompletní schéma do existující produkční databáze.

### 2) Sdílený klíč pro bezpečnou paměť

Vygeneruj **nový, náhodný, jedinečný klíč**, který nikam neposílej. Například v terminálu cPanel:

```bash
python3 -c 'import secrets; print("fai_"+secrets.token_urlsafe(36))'
```

Ve stávajícím `.env` **Enginu** přidej:

```dotenv
FRESH_CHAT_ENGINE_KEY=SEM_NOVY_SAMOSTATNY_KLIC
```

Ve stávajícím `.env` **Chatu** nastav:

```dotenv
FRESH_ENGINE_API_KEY=STEJNY_NOVY_SAMOSTATNY_KLIC
```

Ostatní dosavadní `FRESH_ENGINE_API_KEYS` Enginu, `FRESH_ACCOUNT_SECRET` Chatu a všechny API klíče poskytovatelů **ponech**. Nový klíč nepoužívej pro další aplikace. Bez stejného klíče se může Chat nadále připojit svým dřívějším obecným klíčem, ale nebude aktivní ověřená lokální paměť.

### 3) Nahrání čistých aplikací

**Engine:** Vypni/pozastav nové požadavky, zachovej zálohy, `.env` a hostingové soubory a nahraj obsah ZIPu `fresh-ai-engine-0.5.0-clean.zip` do aplikačního kořene Enginu. V novém Engine balíčku nesmějí být `app.py`, `chat_store.py`, `chat_auth.py` ani kořenové složky Chatu `static/` a `templates/`. Nový `passenger_wsgi.py` musí obsahovat `from flask_app import app as application`. Pokud chceš fyzicky odstranit staré promíchané soubory z hostingu, odstraň je **až po ověření samostatné zálohy**; nemaž cPanel `.htaccess`, produkční `.env` ani adresář cPanel virtualenv.

**Chat:** Nahraj obsah `fresh-ai-chat-0.3.0-clean.zip` do **jiného** kořene – `chat.freshcrackers.cz`. Zachovej Chat `.env`, případný cPanel `.htaccess` a lokální databázi. V Chatu musí `passenger_wsgi.py` nadále používat `from app import application`. Pokud jsou přítomné staré logy či cache, nesmějí být součástí nové ZIP distribuce, ale před smazáním je podle potřeby zazálohuj.

Pokud máš na hostingu jiný startup file než `passenger_wsgi.py` nebo individuální nastavení Python App, **před změnou ho ověř** v cPanelu; nepřekonfigurovávej domény naslepo.

Po nahrání zkontroluj závislosti Enginu (v jeho virtuálním prostředí `pip install -r requirements.txt`) a Chatu (`pip install -r requirements.txt`) podle aktuálního stavu hostingu. Restartuj nejprve Engine, potom Chat.

### 4) Ověření po nasazení

- `https://aiengine.freshcrackers.cz/v1/health` musí vrátit Engine verzi `0.5.0-cpanel-wsgi`.
- `https://chat.freshcrackers.cz/health` musí vrátit Chat verzi `0.3.0`.
- Zkontroluj v Chat administraci existujícího uživatele, profil a původní konverzace. Pak do jednoho Chatu napiš `Zapamatuj si: Kód projektu Alfa je 123.`; odpověď má ukázat `local / memory-v1` při zapnutém zobrazení modelu. V jiném novém Chatu napiš `Co si pamatuješ o kódu projektu Alfa?`; Engine má zopakovat původní poznámku a nevolat poskytovatele. Uložení a výběr lze ověřit na `/account/memory`. Větu poté smaž, pokud byla pouze testovací.
- V Engine `/admin/models` zkontroluj ceny a capabilities; v `/admin/routing-decisions` nebo v nově vytvořených jobech sleduj `task_assignment` a `ordered_candidates`. Test `Analyzuj...` se má vyhodnotit jako `analysis`, `Oprav plugin...` jako `coding` a jednoduchá konverzace jako `general` nebo `chat`. Při současném ceníku **může** běžný Chat stále legitimně vybrat OpenAI; kontroluj důvod volby, ne podíl jednotlivých značek.
- V rámci testu vyzkoušej i přihlášení přes Fresh Crackers a vytvoření osobnosti. Záměrně se nezměnilo SSO, WordPress plugin, WooCommerce produkty ani provizní vazby.

**Rollback:** vrať zazálohované kódy i původní `.env`, restartuj obě aplikace. Nové tabulky `chat_memory` se pro rollback **nemusí mazat**; mohou bezpečně zůstat v databázi bez zásahu do historie. Nepoužívej `DROP DATABASE` ani znovu neimportuj staré kompletní SQL schéma do existujících DB.

## Co bylo ověřeno mimo produkci

Prošla syntaktická kompilace Python souborů a samostatné offline testy routingu, návaznosti úloh, lokální paměti a zamezení použití neověřených kontextů. Nemáme v tomto prostředí přímý přístup k tvému cPanelu, MariaDB, živým API klíčům ani koncovému přihlášení přes Fresh Crackers. Tyto produkční integrační kroky musíš otestovat po nahrání a případné chybové záznamy poslat bez hesel nebo tokenů.
