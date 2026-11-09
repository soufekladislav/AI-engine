# Nasazení Fresh AI Engine 0.5.4

## Co verze řeší

Verze 0.5.4 odděluje routingovou schopnost od konkrétní operace volající aplikace. Požadavek `task_type: "seo.metadata.optimize"`, který ve v0.5.3 končil HTTP 422, je nyní zpětně kompatibilně přijat a normalizován na:

```json
{
  "task_type": "seo",
  "operation": "seo.metadata.optimize"
}
```

Nové konektory mají od začátku posílat obě pole samostatně.

## Nasazení

1. Zálohuj adresář Fresh AI Engine a databázi.
2. Nahraj obsah ZIPu přes stávající v0.5.3. Produkční `.env` a `.htaccess` zachovej.
3. Restartuj Python/Passenger aplikaci.
4. Ověř `/v1/health`; očekávaná verze je `0.5.4-cpanel-wsgi`.
5. Při startu se do tabulky `jobs` automaticky doplní nullable sloupec `operation VARCHAR(160)`. Stávající procesy zůstávají beze změny a mají operaci prázdnou.

Pokud databázový účet nemá právo ALTER, spusť **pouze v databázi Enginu** soubor `SQL-0.5.4.sql` a aplikaci znovu restartuj.

## Doporučený API request

```json
{
  "source": "fresh-seo",
  "project_id": "web-123",
  "task_type": "seo",
  "operation": "seo.metadata.optimize",
  "quality": "balanced",
  "prompt": "Optimalizuj title a meta description pro tuto stránku.",
  "context": {}
}
```

`task_type` určuje potřebnou schopnost modelu. `operation` slouží pro konkrétní business akci, diagnostiku a budoucí nákladové reporty. Engine nevybírá model podle názvu operace, pokud pro ni později nevznikne explicitní pravidlo.

## Zpětná kompatibilita

Starý request:

```json
{"task_type":"seo.metadata.optimize", "prompt":"..."}
```

se přijme bez 422 a interně se uloží jako schopnost `seo` a operace `seo.metadata.optimize`.
