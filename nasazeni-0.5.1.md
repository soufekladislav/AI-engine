# Nasazení Fresh AI Engine 0.5.1 (z aktuální 0.5.0)

1. Zálohuj kompletní adresář a **databázi Enginu**, zvlášť bezpečně uchovej `.env`, `.htaccess`, API klíče a současné cPanel Passenger nastavení. Diagnostický klíč není určen ke zveřejňování.
2. Rozbal obsah ZIPu přímo do existujícího kořene aplikace `aiengine.freshcrackers.cz`, aby byly `flask_app.py`, `passenger_wsgi.py` a adresář `app/` vedle sebe; **nesmí se překrýt adresář Fresh AI Chat**. Zachovej své produkční `.env`/`.htaccess` beze změn.
3. Pro 0.5.0 → 0.5.1 **neprováděj žádnou SQL migraci**. Konfigurace tras, ceníky, oprávnění, účty, kredity a přístupové klíče zůstávají v současné databázi a `.env`.
4. Restartuj Python aplikaci Enginu v cPanelu. Ověř `https://aiengine.freshcrackers.cz/v1/health`: očekávané označení `0.5.1-cpanel-wsgi`. Admin menu už má zobrazovat `v0.5.1` a `diagnostics/manifest.json` má obsahovat `engine_version: 0.5.1`.
5. V administraci v přehledech a seznamech procesů se časy zobrazují v **Europe/Prague** (v září UTC+2, v zimě UTC+1). Externí diagnostický JSON, interní uložená data a API timestampy zůstávají v UTC. Neměň kvůli tomu serverovou časovou zónu ani hodnoty v DB.
6. V `Modely & routing` je vysvětleno, že `mock-local` je **pouze testovací adapter**. Jeho zaškrtnutí neznamená prohledávání konverzací, internet ani inteligentní lokální odpovědi. Ověřovací dotaz nad explicitně uloženou poznámkou uživatele musí při správném přístupovém klíči Chat↔Engine skončit na `local / memory-v1`; pokud záznam nestačí, Engine pokračuje podle modelových tras.
7. Až Engine projde kontrolou, restartuj případně Chat a otestuj předání osobnosti: v Enginu v nové úloze uvidíš aktuální vybraný model, Chat ukáže metadata dokončené odpovědi. Chování GPT/Gemini/Anthropic nelze bez živých API požadavků v offline balíčku garantovat.

### Návrat na 0.5.0

Vrať soubory ze zálohy Enginu a restartuj Passenger. Tento patch nemění tabulky ani hodnoty modelových tras.
