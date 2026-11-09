# Fresh AI Engine 0.8.0 — Fresh Key linking reset

## Cíl
Fresh Key je pouze spojovací vrstva. AI Engine nevytváří pracovní projekty. Jeden Fresh Key řádek představuje jeden společný projekt a obsahuje maximálně jeden lokální projekt z každé Fresh aplikace.

## Nové chování
- Přehled je řádkový a odděluje skutečná Propojení od projektů „Čeká na přiřazení“.
- Stejná URL projektu je silný návrh na sloučení, ale Fresh Key nic neslučuje bez potvrzení uživatele.
- Existující duplicitní Fresh Key řádky lze sloučit. Lokální projekty a pracovní data se nemažou.
- „Připojit další App“ zobrazuje i projekty, které už patří do jiného Propojení. Jejich výběr nabídne sloučení místo prázdného seznamu.
- Jeden Fresh Key může obsahovat maximálně jeden projekt z Lindy, jeden z Bobana, jeden z Edy atd.
- Lokální projekt lze z Propojení odpojit bez smazání projektu v aplikaci.
- Linda 0.4.8.7 používá duplicate-safe publish v2. Starší aplikace zůstávají kompatibilní s původním API a lze je aktualizovat postupně.

## Databáze
0.8.0 nepřidává nové tabulky ani sloupce. Stávající data se zachovávají. Duplicitní Propojení se opravují přes UI funkcí Sloučit.

## Nasazení
1. Zachovat produkční `.env` a databázi.
2. Nahrát obsah balíčku přes stávající aplikaci AI Engine.
3. Restartovat pouze AI Engine.
4. Ověřit `/v1/health` a přihlášení zákaznického účtu.
5. Otevřít `Účet → Propojení`.
6. Pokud se objeví „Doporučená oprava“, nejprve sloučit zjevné duplicity se stejnou URL (např. Adrenaline Race Linda + Boban).
7. Zkontrolovat, že Fresh Crackers a CajnaCars zůstávají po jednom řádku s více aplikacemi.
8. Projekty v „Čeká na přiřazení“ připojit k nalezenému Propojení nebo vytvořit samostatný Fresh Key.

## Doporučený test
- Adrenaline Race: sloučit Linda + Boban → jeden řádek.
- Fresh Crackers: jeden řádek Linda + Boban.
- CajnaCars: jeden řádek Linda + Boban.
- Fresh Develop: zatím Linda; vytvořit samostatné Propojení nebo později připojit další App.

## Rollback
Protože 0.8.0 nemění schéma, rollback znamená pouze vrátit soubory předchozí verze. Operace Sloučit/Odpojit mění pouze Fresh Key vazby, ne pracovní data v aplikacích.
