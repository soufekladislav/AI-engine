# Nasazení Fresh AI Engine 0.5.5

## Smysl verze

0.5.5 řeší problém, kdy Engine sice přijal operaci například `seo.metadata.optimize`, ale provider neviděl všechna aplikační data a Engine považoval libovolnou textovou odpověď modelu za úspěšný výsledek.

Nově Engine rozlišuje:

1. **schopnost** – `task_type`,
2. **operaci** – `operation`,
3. **aplikační data** – `input_data`,
4. **požadovaný výsledek** – `output_contract`.

## Nasazení z 0.5.4

1. Zálohuj adresář aplikace a databázi Enginu.
2. Nahraj obsah `fresh-ai-engine-0.5.5-clean.zip` přes současnou 0.5.4.
3. Zachovej produkční `.env`, `.htaccess` a funkční `passenger_wsgi.py`, pokud jej hosting upravuje individuálně.
4. Restartuj Python / Passenger aplikaci.
5. Ověř `GET /v1/health`; očekávaná verze je `0.5.5-cpanel-wsgi`.
6. Při startu se tabulka `jobs` automaticky rozšíří o sloupce pro dočasná aplikační data, výstupní kontrakt a validační metadata.

Pokud databázový účet nemá právo `ALTER TABLE`, spusť **pouze v databázi Fresh AI Engine** soubor `SQL-0.5.5.sql` a aplikaci restartuj.

## Zpětná kompatibilita Bobana / Web Boosteru

Současný konektor nemusí být kvůli prvnímu testu okamžitě přepsán:

- legacy `task_type: "seo.metadata.optimize"` se stále normalizuje na `seo` + operaci,
- data, která konektor nyní posílá v běžném `context`, jsou nově providerovi předávána jako aplikační data,
- pro operaci `seo.metadata.optimize` Engine automaticky aktivuje vestavěný JSON kontrakt.

Pro další vývoj je ale doporučené přejít na explicitní `input_data`.

## Normalizovaná synchronní odpověď

Při úspěšném strukturovaném výsledku vrací `/v1/engine/run` například:

```json
{
  "job_id": "...",
  "status": "completed",
  "task_type": "seo",
  "operation": "seo.metadata.optimize",
  "provider": "openai",
  "model": "...",
  "output": "{\"items\":[...]}",
  "result_type": "json",
  "result": {
    "items": [
      {
        "issue_id": 123,
        "post_id": 456,
        "changes": {
          "title": "...",
          "description": "..."
        }
      }
    ]
  },
  "result_validated": true,
  "schema_id": "seo.metadata.optimize.v1"
}
```

Pole `output` zůstává kvůli zpětné kompatibilitě. Nové konektory mají používat především `result`.

## Vlastní JSON kontrakt pro další aplikace

Konektor může poslat vlastní omezené JSON schema bez změny Enginu:

```json
{
  "task_type": "analysis",
  "operation": "analysis.report.create",
  "prompt": "Vytvoř souhrn dat.",
  "input_data": {"rows": []},
  "output_contract": {
    "type": "json",
    "schema": {
      "type": "object",
      "required": ["summary"],
      "properties": {
        "summary": {"type": "string", "minLength": 1}
      }
    }
  }
}
```

Podporovaná validační podmnožina: `type`, `required`, `properties`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minProperties`, `maxProperties`, `enum`, `const`, `anyOf`, `allOf` a `additionalProperties: false`.

## Co se děje při chybném výstupu modelu

Pokud model vrátí obyčejný text místo požadovaného JSON nebo struktura nesplní schema:

1. první pokus se uloží jako `invalid_output`,
2. Engine jednou požádá stejnou route o opravu,
3. pokud ani oprava neprojde, může použít další způsobilou route,
4. pokud žádná route kontrakt nesplní, job skončí jako `failed` s chybou `structured_output_invalid`.

V detailu procesu je vidět výstupní kontrakt, validační stav a jednotlivé pokusy.

## Bezpečnost a data

`input_data` a obecná aplikační data jsou providerovi jasně označena jako nedůvěryhodná **DATA**, nikoli systémové instrukce. Pokud je `STORE_PAYLOADS=false`, Engine je drží jen po dobu zpracování jobu a následně je z pracovních polí odstraní. Metadata kontraktu a validační stav zůstávají kvůli diagnostice.
