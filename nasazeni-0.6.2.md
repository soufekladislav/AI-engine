# Fresh AI Engine 0.6.2 – nasazení image dispatch

## Co 0.6.2 opravuje

Verze 0.6.2 odděluje `image.generate` od generického textového routeru. Operace má přednost před `task_type`, takže i kompatibilní požadavek Lindy `task_type=general + operation=image.generate` se uloží jako capability `image_generation` a jde výhradně přes image provider adapter.

`POST /v1/engine/run` a `POST /v1/media/image/generate` používají stejný interní job/execution path. Úspěšný výsledek má `result_type=image` a `result.kind=image`; obrázek je vrácen jako `base64` nebo URL. Binární payload se neukládá do DB, ale metadata ano.

Textové route (`openai-fast`, Anthropic, DeepSeek, Gemini text) nejsou pro `image.generate` kandidáti ani fallback. Image failover se může odehrát pouze mezi adaptéry v image-provider registry. V 0.6.2 je připojen OpenAI Images API; registry je připravené pro dalšího obrazového providera.

## DB / kompatibilita

- **Bez SQL migrace.** Používají se stávající `jobs`, `job_attempts`, `routing_decisions`, `job_billing` a `runtime_settings`.
- Stávající `.env` zachovat.
- Přibyla volitelná proměnná `OPENAI_IMAGE_MODEL`. Pokud v `.env` není, výchozí je `gpt-image-2.5-flare`.
- Stávající textové API a konektory Linda, Boban, Eda, Chat a Karel zůstávají kompatibilní.
- `run_id` je nový alias; původní `job_id` zůstává v odpovědi.

## Přesný update na cPanel / Passenger

1. Zazálohuj aktuální Application Root 0.6.1 a databázi. `.env` nemaž ani nepřepisuj.
2. Rozbal `fresh-ai-engine-0.6.2-clean.zip` lokálně nebo na serveru.
3. Nahraj **obsah** balíčku do skutečného Passenger Application Root a přepiš aplikační soubory. `.env` na serveru ponech beze změny.
4. Smaž staré `__pycache__` / `.pyc`, pokud na serveru zůstaly z předchozí verze.
5. V aktivním virtualenv spusť:

```bash
pip install -r requirements.txt
python -m compileall -q app flask_app.py passenger_wsgi.py cron_worker.py
```

6. Restartuj Passenger aplikaci v cPanelu. Pokud používáš restart file:

```bash
mkdir -p tmp
touch tmp/restart.txt
```

7. Ověř health:

```bash
curl -s https://aiengine.freshcrackers.cz/v1/health
```

Očekávání:

```json
{
  "version": "0.6.2-cpanel-wsgi",
  "router": "2.3.0-media-dispatch"
}
```

## Produkční acceptance test – 1 placené image volání

Spusť **v Application Rootu a v produkčním virtualenv**. Skript načte existující Engine API key z lokálního `.env`; klíč nevypisuje ani ho nemusíš kopírovat do terminálu.

```bash
python scripts/acceptance_image_api.py --url https://aiengine.freshcrackers.cz
```

Test záměrně posílá kompatibilní request Lindy:

- `source=Linda app`
- `task_type=general`
- `operation=image.generate`
- `quality=balanced`

Kontroluje HTTP 200, `status=completed`, `task_type=image_generation`, `result_type=image`, `result.kind=image`, dekódovatelný obrázek, MIME, skutečný image model, audit přes `/v1/engine/jobs/<run_id>`, nenulový náklad a to, že nebyl použit textový model.

Úspěšný konec je:

```text
PASS image.generate production acceptance
```

## Manifest po acceptance testu

Vygeneruj/obnov diagnostický manifest. `media_capabilities.image_generation` nyní obsahuje:

- `providers` – provider, image model a adapter,
- `endpoint` a `shared_adapter=true`,
- `adapter_status`,
- `success_calls`, `failed_calls`,
- `legacy_text_misroutes` – historické staré chybné requesty, které byly dokončeny jako text,
- `last_run` – source, capability, operation, provider, image model, MIME, rozměry, bytes, cost, latency a error category.

Po novém úspěšném testu musí `last_run.result_type` být `image` a model nesmí být běžný textový model.

## Náklady a kredity

Image provider usage se zapisuje do `job_attempts` a `jobs.estimated_cost_usd`. Pokud provider vrátí token usage, Engine počítá náklad z image/text tokenů; pokud usage chybí, použije transparentně označený fallback odhad (`cost_source=fallback_per_image_estimate`). Customer connector používá standardní rezervaci a settlement kreditů přes `job_billing`.

Pro `image.generate` už tedy neplatí staré `501 Customer media billing is not enabled yet`.

## Failure semantics

- žádný image provider dostupný: `status=failed`, `result_type=image`, `error_category=image_generation_unavailable`;
- image provider byl zavolán, ale žádný nevytvořil použitelný asset: `image_generation_failed`;
- překročen `max_cost_usd` před voláním: `image_generation_budget_exceeded`;
- textové vysvětlení se nikdy nepovažuje za úspěšný image výstup.

## Regression kontrola

V 0.6.2 nebyl měněn standardní textový execution loop Routeru 2.2/2.3. Lokálně před sestavením balíčku prošly:

- operation override `general + image.generate -> image_generation`,
- image success + base64 decode,
- image unavailable,
- image provider failure bez textového fallbacku,
- tvorba běžného `chat.conversation` jobu,
- tvorba `marketing.social.create` structured JSON jobu,
- kompletní `compileall`.

Po nasazení doporučuji z aplikací pouze ověřit stávající běžný tok `chat.conversation`, `marketing.social.create`, `marketing.article.create` a `analysis.project_profile`; jejich textový router nebyl nahrazen image dispatch logikou.

## Rollback

Rollback je jednoduchý: vrať soubory 0.6.1 a restartuj Passenger. DB rollback není potřeba, protože 0.6.2 nemění schema.
