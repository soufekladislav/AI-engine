# Fresh AI Engine 0.8.1 – náklady a kredity (etapa 01)

## Co se mění

- `app/cost_models.py` – nové, nezávislé auditní tabulky (verze tarifů, pokusy, neměnné účetní podmínky při rezervaci, jednoznačné vyúčtování).
- `app/cost_accounting.py` – výpočet a sledovatelnost nákladů, atomické rezervace a vyúčtování kreditů, ochrana proti souběžnému přečerpání.
- `app/models.py` – registrace nových tabulek při stávajícím `Base.metadata.create_all`.
- `app/accounts.py` – bezpečné `bind_job` a `settle_job` při zachování původního API.
- `app/engine.py` – verzovaný ceník před textovým voláním, preflight kontrola rozsahu výstupu; účtování vision usage podle explicitního ceníku.
- `flask_app.py` – zachování starého i nového ceníku při úpravě modelu, vyřešení souběžné rezervace, chráněný přehled a nastavení vision tarifů.

**Původní tabulky ani zákaznická data se nemažou.** Přidávají se pouze čtyři nové tabulky:
`provider_tariff_versions`, `attempt_cost_records`, `job_billing_terms`, `job_settlement_records`.
Připojení Fresh Key / Fresh Account a stávající API zůstávají beze změny.

## Důležitá upozornění

1. Soubor `Fresh-AI-Engine-0.8.1-changed-files.zip` generuje skript `build_release.py`. Tento balíček sám **není** určen k prostému nahrání do produkce. Je nutné nejprve spustit generátor, který načte přesně referenční veřejnou verzi GitHubu a připraví kompletní dotčené soubory.
2. Před nasazením zálohujte databázi **MariaDB** i aktuální soubory. Neprovádějte změny přímo v produkci bez stagingového testu.
3. `CREATE TABLE` vyžaduje právo vytvářet tabulky. Startup bude u nových tabulek volat `Base.metadata.create_all()`; žádné existující tabulky se nemažou.
4. GitHub výchozí větev zůstane nedotčená, dokud **vy sami** nenahrajete vygenerované soubory.
5. Kód je navázaný na **commit 717d5a67b085b4574ce1cbfcc1118ae683aba8de** (repozitář `soufekladislav/AI-engine`). Novější soubory generátor odmítne přepsat.
6. Výstupní finanční marže je **nominální odhad** podle nastavených tarifů a provider usage, nikoli bankovně/fakturačně ověřený výsledek. Reconciliace s fakturami API poskytovatelů zůstává samostatnou navazující funkcí.
7. Cena `image.understand` zůstane **neznámá** a marže nevypočtená, dokud není zadán ověřený tarif podle konkrétního modelu. Zákaznická vision operace již byla ve výchozí aplikaci blokována (HTTP 501); tímto balíčkem ji nezapínáme.
8. `max_cost_usd` používá konzervativní odhad vstupních tokenů a maximálního výstupu; není to záruka fakturované ceny od poskytovatele. Pro zákaznické kredity však platí pevný strop odpovídající rezervaci. Případné přečerpání skutečných nákladů nese platforma a je zaznamenáno.
9. I úspěšná žádost může mít `unknown_attempts`, zejména po timeoutu API bez usage. V takovém případě marže není vydávána za spolehlivou.

## Vytvoření ZIPu s úplnými soubory

Na Windows stačí spustit `VYTVORIT_ZIP.bat`, případně v terminálu:

```bash
python build_release.py --download
```

Skript stáhne veřejný referenční commit GitHubu, ověří soubory Git blob hash a vytvoří:

`Fresh-AI-Engine-0.8.1-changed-files.zip`

Nebo přímo z vašeho lokálního klonu:

```bash
python build_release.py --repo C:\\cesta\\k\\AI-engine
```

Výstupní ZIP obsahuje **celé** upravené soubory se správnými cestami a můžete jej použít jako podklad pro commit do GitHubu.

## Staging a ostré nasazení

1. Vygenerovat výstupní ZIP a založit GitHub větev `feature/billing-081`.
2. Nahrát pouze soubory z vygenerovaného ZIPu se zachováním adresářů.
3. Na stagingu udělat zálohu MariaDB a aplikace.
4. Nasadit a restartovat Passenger; ověřit inicializaci čtyř nových tabulek.
5. Ověřit `/v1/health`, `/v1/engine/route`, základní textový request a administraci `/admin/economics-v2`.
6. Ověřit jeden úspěšný placený job, jeden neúspěšný job a dvě souběžné rezervace v rámci jednoho testovacího účtu.
7. Porovnat záznamy `credit_ledger`, `job_billing`, `job_settlement_records`, `attempt_cost_records`.
8. Teprve po úspěšném stagingovém testu nasadit do produkce. Zachovat stávající `.env`.

## Nastavení ceny modelu pro vision

Nejdříve zjistěte aktuální **ověřenou cenu** konkrétního modelu podle oficiálního ceníku poskytovatele. V administrátorské autentizované session pošlete JSON na:

`POST /admin/media-tariff`

```json
{
  "provider": "openai",
  "model": "ZDE-KONKRETNI-MODEL",
  "input_usd_per_million": 0,
  "output_usd_per_million": 0
}
```

Pozor: výše uvedené nuly jsou pouze šablona polí a server je úmyslně odmítne jako neplatný tarif. Doplňte skutečné platné sazby.

Historie verzí se po změně nemění zpětně. Čtení obrázků potřebuje také skutečné usage od API. Pokud model používá oddělené ceny za obrazový vstup, je třeba rozšířit metering na tyto jednotky před zapnutím účtování zákazníkům.

## Reporting

`GET /admin/economics-v2?limit=100` (chráněná administrace)

Vrací souhrn pouze za zobrazené poslední joby, seznam pokusů a stavy odhadovaných nákladů. `gross_margin_czk` je `null`, pokud není znám kompletní náklad nebo kurz. `platform_subsidy_credits` ukazuje chybějící část účtování, když skutečný náklad překročí rezervaci.

## Návrat na předchozí verzi

1. Zastavte worker (a nedovolte nová zpracování během rollbacku).
2. Obnovte původní verzi čtyř nahrazených souborů (repozitář `717d5a67...`) a odstraňte dva nové moduly.
3. Restartujte Passenger. Nové čtyři tabulky **nemažte**; lze je bezpečně ponechat pro pozdější opětovné nasazení.
4. V případě komplikací konzultujte rozdíly `credit_ledger` a nové záznamy před opakovaným zúčtováním.

## Testy

Balíček obsahuje izolované SQLAlchemy testy (`pytest -q tests`). Proběhly se SQLite souborovou databází pro transakční závod dvou rezervací, idempotentní settlement, neúspěšný placený výstup, neznámý vision tarif, neměnnost verzí a přečerpání rezervace.

**Nebylo provedeno live volání API poskytovatelů ani integrační test na produkční MariaDB/Passenger.** Tento krok je nutný na stagingu.
