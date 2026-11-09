# Nasazení Fresh AI Engine 0.6.1

Verze 0.6.1 doplňuje ochranu proti výpadku nebo blokaci celé AI platformy. Primární model se stále vybírá před prvním provider voláním; další platforma se používá pouze po skutečném selhání nebo po nevalidním povinném výstupu.

## Co je nové

- Cross-provider failover pro text / chat / analýzu / marketing / SEO / coding.
- Až 4 provider volání pouze při technickém failoveru (billing/credit, auth, rate-limit, timeout/network, 5xx nebo jiná provider chyba).
- Nevalidní obsah zůstává nákladově omezen na nejvýše 2 obsahové pokusy.
- Billing/credit chyba provideru otevře `provider guard`; provider se dočasně vyřadí z Routeru a další úlohy rovnou použijí jinou platformu.
- Cooldown podle typu chyby: billing/credit 2 h (při opakování déle), auth 6 h, rate-limit typicky 5 min / Retry-After, 5xx 3 min, síť/timeout 2 min.
- Po úspěšném half-open testu se guard automaticky smaže.
- Admin dashboard zobrazuje aktivní provider guardy a provozní upozornění.
- Po úspěšném failoveru vznikne upozornění „Úloha dokončena přes záložní platformu“ s odkazem na detail jobu.
- Neúspěšná úloha s evidovaným nákladem alespoň 0,05 USD vytvoří upozornění „Náklad bez použitelného výsledku“.
- Z dashboardu lze provider ručně odblokovat po dobití kreditu / opravě API klíče.
- API response nově obsahuje `fallback_used`, `initial_provider`, `final_provider`, `recovery_trigger`, `provider_calls`.
- Structured-output circuit breaker se otevírá už po 3 po sobě jdoucích nevalidních výstupech stejné route/operace (dříve 5).
- Recovery v `auto/economy/balanced` preferuje známou úspěšnost a rozumnou cenu; po selhání levného provideru se tedy bez důvodu neskáče rovnou na výrazně dražší platformu.

## Důležité omezení

Cross-provider failover se v 0.6.1 týká textových/modelových operací. `image.generate` a `image.understand` mají zatím pouze OpenAI media adaptér, takže při jeho billing/auth výpadku Engine provider označí jako nedostupný a upozorní administrátora, ale obrazovou úlohu nemá kam přepnout. Pro skutečný image failover je potřeba přidat druhý media provider.

## Databáze

Z 0.6.0 na 0.6.1 není potřeba SQL migrace. Provider guardy i admin upozornění se ukládají do stávající tabulky `runtime_settings`.

## Nasazení

1. Zálohuj aktuální adresář aplikace a `.env`.
2. Zachovej produkční `.env` a hostingový `.htaccess`, pokud obsahuje lokální cPanel pravidla.
3. Nahraj obsah balíčku 0.6.1 do skutečného Passenger Application Root.
4. Restartuj Passenger.
5. Ověř `/v1/health`.

Očekávaný výstup obsahuje přibližně:

```json
{
  "ok": true,
  "version": "0.6.1-cpanel-wsgi",
  "router": "2.2.0-provider-failover-guard",
  "provider_failover": true,
  "provider_guard": true,
  "structured_recovery": true
}
```

## Doporučený test po nasazení

1. Spusť běžný článek / analýzu a ověř jeden provider call.
2. Dočasně použij neplatný testovací klíč jednoho provideru nebo route ručně otestuj tak, aby provider vrátil autorizaci/billing chybu.
3. Úloha má pokračovat přes jinou dostupnou platformu.
4. V `/admin` se má objevit provider guard a upozornění na failover.
5. Detail jobu má ukázat jednotlivé provider pokusy a `recovery_trigger`.
6. Po opravě kreditu/klíče můžeš v dashboardu použít „Ověřit znovu / odblokovat“, nebo počkat na automatický cooldown.

Nepoužívej produkční klíč k záměrnému vyčerpání kreditu jako test.
