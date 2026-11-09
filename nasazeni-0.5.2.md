# Fresh AI Engine 0.5.2 – aktualizace z 0.5.1

1. Zálohuj databázi Enginu i aktuální adresář aplikace; uchovej produkční `.env` a `.htaccess`. Tento balíček obsahuje jen Engine, Chat 0.3.1 neměň.
2. V cPanelu nahraj obsah ZIPu přímo do stávajícího aplikačního kořene `aiengine.freshcrackers.cz`, soubory aplikace přepiš; **neodstraňuj stávající databázi, .env, .htaccess ani aplikační virtuální prostředí**. Ponech existující cestu Passengeru.
3. Při startu se opakovatelně doplní čtyři nullable sloupce tabulky `job_billing`. Pokud DB uživateli chybí ALTER oprávnění, spusť po záloze ručně SQL ze souboru `SQL-0.5.2.sql` v **databázi Enginu**, pak restartuj. **Pokud automatické rozšíření již proběhlo, SQL znovu nespouštěj.**
4. Restartuj aplikaci v cPanelu. `/v1/health` má uvádět `0.5.2-cpanel-wsgi`, v administraci `v0.5.2`. Otevři `Zákaznické účty` → `Účtování kreditů`, nastav ruční orientační kurz Kč/USD pro účetní přehled a ulož. Aktuální nominální cena jednoho kreditu se jen čte; skutečnou cenu a DPH určuje e-shop.
5. V detailu účtu vyzkoušej souhrn a rozbalení jednoho projektu. Projekty bez ID zůstávají odděleně podle aplikace/konektoru. Pokud je neznámý kurz, nenaceněná externí úloha nebo nulová nominální hodnota, marže se nezobrazí.

## Omezení výpočtu
- Celková hodnota zůstatku = disponibilní kredity × současná informativní cena kreditu. Spotřebovaná nominální hodnota = skutečně zaúčtované kredity × cena kreditu z okamžiku vyúčtování; u starších úloh se použije aktuální cena a přehled to označí.
- Náklad v USD pochází z `job_billing.cost_usd` uloženého při dokončení úlohy, tokeny ze `jobs`. Náklad v Kč je násobek ručně vloženého kurzu; nová úloha uloží kurz v okamžiku vyúčtování. Pokud kurz není vložen, Kč náklad a marže zůstávají neznámé.
- **Modelový rozdíl a marže nejsou skutečný profit!** Nezohledňují skutečnou nákupní cenu kreditů, DPH, slevy, neúspěšné API pokusy, image/video, provoz serveru ani jiné režijní náklady. Bez data prodejní objednávky nelze spolehlivě přičíst skutečnou tržbu projektu.
- Detail providerů zobrazuje historické sazby pro nově vyúčtované požadavky; pro staré řádky aktuální tarif označený jako orientační, případně označí smíšené sazby.

## Návrat
Při chybě obnov soubory předchozí verze 0.5.1 a restartuj. Nové nullable sloupce v DB lze ponechat, předchozí verzi neomezují.
