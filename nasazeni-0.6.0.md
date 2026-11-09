# Nasazení Fresh AI Engine 0.6.0

## Co verze řeší

0.6.0 je funkční oprava pro Lindu a další aplikace nad Engine:

1. nativní JSON režim providerů pro strukturované výstupy,
2. circuit breaker pro opakovaně nevalidní route,
3. maximálně jeden recovery provider call pouze po technickém/structured failu,
4. konzervativní lokální JSON repair bez domýšlení useknutého obsahu,
5. `image.generate` a `image.understand` přes standardní synchronní `/v1/engine/run`,
6. kompatibilita `input_data=null` a starého `output_contract.type=object|array`,
7. `marketing.article.create` má minimální výstupní limit 4096 tokenů, aby dlouhý JSON nekončil useknutý na původním defaultu 1200.

## Nasazení na cPanel / Passenger

1. Udělej zálohu aktuální aplikace a DB.
2. Zachovej produkční `.env` a hostingovou `.htaccess` konfiguraci.
3. Nahraj celý obsah balíčku 0.6.0 do skutečného Passenger Application Root.
4. Restartuj Passenger (`tmp/restart.txt` nebo cPanel Restart Application).
5. Ověř `/v1/health`.

Očekávané hodnoty:

```json
{
  "version": "0.6.0-cpanel-wsgi",
  "router": "2.1.0-bounded-structured-recovery",
  "structured_recovery": true,
  "image_generation": true
}
```

`image_generation` bude `false`, pokud v produkčním `.env` není platný `OPENAI_API_KEY`.

## Databáze

Pro přechod z 0.5.9 není potřeba žádná nová DB migrace. 0.5.9 už obsahuje potřebné LONGTEXT úpravy.

## Doporučený test po nasazení

### 1. Linda – článek

Spusť jednu `marketing.article.create` úlohu. V routing trace očekávej buď přímé dokončení, nebo maximálně 2 provider calls. Route s čerstvou sérií invalidních JSONů musí mít `structured_output_circuit_open` a nesmí být rank #1.

### 2. Linda – obrázek

Pošli synchronně:

```json
{
  "source": "Linda app",
  "task_type": "general",
  "operation": "image.generate",
  "quality": "auto",
  "prompt": "Minimalistický vizuál pro sociální síť, zelený akcent, bez textu",
  "input_data": {"size": "1024x1024"}
}
```

Očekávej `status=completed`, `result_type=image` a `result.image_base64` nebo `result.image_url`.

### 3. Linda – starší request kompatibilita

`input_data: null` už nemá vracet 422. Stejně tak se `output_contract: {"type":"object"}` převede na JSON objekt.

## Důležitá hranice

0.6.0 opravuje Engine. Lindin scheduler/dedupe a pravidlo „WordPress publish nesmí být blokovaný AI enrichmentem“ jsou změny v samotné Lindě a vyžadují její zdrojový balíček. Engine po této verzi ale už nemá být hlavním důvodem současné série `structured_output_invalid` a image jobs už nemají padat do textového routeru.
