# Fresh AI Engine 0.7.4 – Fresh Key zdarma a aktivace z aplikací

Fresh Key je bezplatná spojovací vrstva. Kredity nejsou potřeba pro Fresh Key ani pro správu účtu AI Enginu.

## Novinky

- nový server-to-server endpoint `POST /v1/fresh-key/account/bootstrap`;
- Linda, Boban, Eda, Karel a další důvěryhodné aplikace mohou připravit Fresh Key identitu a lehký účet AI Enginu bez kreditu;
- první následné přihlášení přes ověřený Fresh Account bezpečně připojí centrální identitu k tomuto účtu;
- role jsou sjednocené na Vlastník / Správce / Editor / Čtenář; označení Klient / Agentura / Člen týmu je lokální vztah, nikoli oprávnění;
- AI Engine zůstává pouze centrálním přehledem. Lokální role a pracovní data řídí konkrétní aplikace.

## Nasazení

1. Zachovej produkční `.env` – zejména `FRESH_ACCOUNT_*`, `FRESH_KEY_*`, databázi a API klíče.
2. Přepiš aplikační soubory verzí 0.7.4.
3. Databázové změny z 0.7.3 zůstávají platné; 0.7.4 nepřidává novou povinnou tabulku.
4. Restartuj Passenger.
5. Ověř `/admin/diagnostics`, zákaznický login a `/account/fresh-key`.

Důležité: aplikace se pouze registruje jako identita uživatele. Lokální projekt se do Fresh Key neodesílá automaticky – musí uživatel výslovně použít `Odeslat do Fresh Key`.
