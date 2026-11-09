# Fresh AI Engine – univerzální Developer API

## Zásada

Všechny aplikace (Fresh Security, Linda, Boban, Eda, Karel i další) používají stejné API. Nová aplikace **nevyžaduje** vlastní endpoint ani serverový konektor v AI Engine. Potřebuje pouze serverového HTTPS klienta a příslušný API klíč. Identita/Fresh Key a aplikacemi specifická oprávnění zůstávají odděleny od samotné AI inference.

## Rozhraní

- `GET /v1/health` – stav služby (bez klíče).
- `POST /v1/engine/run` – synchronní úloha.
- `POST /v1/engine/jobs` – asynchronní úloha.
- `GET /v1/engine/jobs/{job_id}` – výsledek úlohy.
- `POST /v1/engine/route` – routing; není nutný pro běžné inference.

Base URL: `https://aiengine.freshcrackers.cz`
Hlavičky: `Authorization: Bearer <API_KEY>` a `Content-Type: application/json`.

Klíč vystav v zákaznickém účtu v `API konektory`. Uchovávej pouze na backendu volající aplikace, nesdílej mezi zákazníky ani neserializuj do diagnostiky.

## Datový kontrakt

Používej `task_type` dle dostupných schopností, `prompt`, identifikátor `source`, volitelné `operation`, `project_id`, `input_data`, `context`, `quality` a `output_contract`. `operation` slouží k rozlišení práce, nikoli k vytvoření endpointu. Při synchronním volání ověř výsledek; při asynchronním ulož `job_id` a polluj s exponenciálním backoff. Nevytvářej po timeoutu automaticky duplicitní placenou úlohu.

Strojová dokumentace: `/account/developer-api/openapi.json` pro přihlášeného uživatele, `/admin/developer-api/openapi.json` pro administrátora.

## Zabezpečení

HTTPS, oddělené klíče, minimalizace dat a redakce tajných údajů, limity objemu, ochrana proti prompt injection, kontrola JSON výstupu, žádné vykonávání AI návrhů bez další autorizace.

## Poznámky

API dokumentace popisuje skutečně existující endpointy a neslibuje novou capability ani změnu routingu. Uživatelé používají standardní AI přístup; zřízení další aplikace samo o sobě nevyžaduje programovou změnu Engine.
