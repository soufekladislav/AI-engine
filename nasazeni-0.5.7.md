# Nasazení Fresh AI Engine 0.5.7

Tato verze navazuje na 0.5.6, ale lze ji použít i jako úplné překrytí instalace 0.5.4/0.5.5.

## Důležité z aktuální diagnostiky

Manifest z produkce generovaný 30. 9. 2026 stále hlásí `engine_version: 0.5.4`. To znamená, že předchozí balíček 0.5.6 nebyl na aktivním Passenger rootu kompletně načten, případně běží starý adresář/proces. Proto tentokrát nepřepisuj jen jednotlivé soubory.

## Postup

1. Zálohuj aktuální adresář aplikace a MariaDB.
2. Zachovej produkční `.env`, `.htaccess` a nastavení Passenger Application Root.
3. Smaž/odsuň starý aplikační kód kromě produkčních tajných konfiguračních souborů a nahraj celý obsah `fresh-ai-engine-0.5.7-clean.zip` do skutečného Passenger Application Root.
4. Restartuj Passenger. Pokud cPanel používá `tmp/restart.txt`, dotkni se souboru až po kompletním uploadu.
5. Ověř `/v1/health`. Musí obsahovat:
   - `version: 0.5.7-cpanel-wsgi`
   - `router: 2.0.1-single-shot-failsafe`
6. Vygeneruj nový diagnostický manifest. Musí mít `engine_version: 0.5.7` a blok `router.version: 2.0.1`.
7. Teprve potom spusť Bobanův `seo.metadata.optimize`.

## Databáze

0.5.7 nepřidává nový povinný sloupec oproti 0.5.6. Pokud instalace ve skutečnosti zůstala na 0.5.4, je ale potřeba mít schéma 0.5.5/0.5.6: vstupní data/kontrakt, `model_routes.routing_profile_json` a tabulku `job_feedback`. Automatický start je vytváří pomocí `create_all()` + opakovatelných upgrade kroků.

`SQL-0.5.7.sql` je pouze fallback. Před ručním spuštěním ověř, které sloupce/tabulky už existují.

## Co 0.5.7 opravuje

- historie Routeru nikdy nesmí způsobit HTTP 500,
- rezervace kreditů je fail-safe,
- neočekávaná chyba po vytvoření jobu se vrátí jako normální `status=failed` s bezpečnou kategorií,
- worker nenechá staré `claimed/running` joby viset navždy,
- stále platí single-shot princip: standardně 1 úloha = 1 vybraný provider call; žádné pokus–omyl přeskakování modelů.

## Boban

Doporučený request zůstává:

```json
{
  "task_type": "seo",
  "operation": "seo.metadata.optimize",
  "quality": "auto",
  "prompt": "Optimalizuj metadata vybraných stránek.",
  "input_data": {"pages": []},
  "output_contract": {"type": "json", "schema_id": "seo.metadata.optimize.v1"}
}
```

Legacy `balanced` je stále podporován, ale pokud má rozhodovat Router podle náročnosti a ekonomiky, konektor by měl používat `auto`.
