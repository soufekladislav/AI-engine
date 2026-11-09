# Nasazení Fresh AI Engine 0.5.8

## Před aktualizací

1. Zálohuj databázi a současný Application Root.
2. Zachovej produkční `.env` a případný hostingový `.htaccess`.
3. Tato verze **nevyžaduje SQL migraci** oproti 0.5.7.

## Nasazení

1. Nahraj celý obsah čistého balíčku 0.5.8 do skutečného Passenger Application Root.
2. Nepřepisuj produkční `.env`.
3. Restartuj Passenger aplikaci.
4. Otevři `/v1/health` a ověř:
   - `version: 0.5.8-cpanel-wsgi`
   - `router: 2.0.1-single-shot-failsafe`
5. V administraci otevři **Manifest**, vytvoř/aktualizuj snímek a zkontroluj lokální čas Praha.
6. Otevři **Náklady & provoz** a porovnej stejné časové období s provider dashboardem. Hlavní metrika v 0.5.8 zahrnuje známé náklady provider callů z `job_attempts`, nikoli jen dokončené joby.
7. V zákaznickém Accountu ověř mobilní menu, Spotřeba & provoz, klikací aplikace a pohyby kreditů.

## Co čekat u rozdílu OpenAI vs. Engine

0.5.8 opravuje jednu konkrétní mezeru: známý tokenový náklad nevalidního AI výstupu se nyní objeví v provider-cost pohledu, i když výsledný job skončil jako failed. Přesto nemusí částka přesně odpovídat provider faktuře. Technický request, který skončí před vrácením tokenů, může mít v Enginu neznámý náklad. Rozdíl může způsobit i jiné období, jiný API projekt/klíč nebo nesoulad tarifů.

## Admin-console

`admin-console` je provoz vytvořený přes stránku **Test úlohy**. Každý test je skutečné provider API volání. Tento provoz je interní, není rozpočítaný do zákaznických účtů a v Náklady & provoz má vlastní metriku.
