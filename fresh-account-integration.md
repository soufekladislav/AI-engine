# Fresh Account workflow pro další Fresh aplikace

Stejný model lze použít pro Fresh AI Chat, Lindu, Karla, Viki a další aplikace.

## Zásady
- hesla zůstávají ve WordPressu FreshCrackers.cz a mezi aplikacemi se nesynchronizují,
- každá aplikace má vlastní `app_id`, tajný klíč, callback a seznam produktů,
- SSO používá jednorázový autorizační kód s krátkou platností,
- aplikace získá jen objednávky produktů, které jsou jí přiřazené,
- provozní objednávka obsahuje právě jeden produkt,
- služba se aktivuje až po uhrazení a úspěšném callbacku,
- opakovaný callback stejné objednávky nesmí službu připsat dvakrát.

## API konektoru
- `POST /wp-json/fresh-account/v1/auth/password`
- `GET /wp-json/fresh-account/v1/auth/sso/start`
- `POST /wp-json/fresh-account/v1/auth/exchange`
- `POST /wp-json/fresh-account/v1/account/register`
- `POST /wp-json/fresh-account/v1/account/verification/resend`
- `POST /wp-json/fresh-account/v1/account/password/forgot`
- `GET /wp-json/fresh-account/v1/orders`
- `POST /wp-json/fresh-account/v1/checkout`

Server-to-server endpointy používají hlavičky `X-Fresh-App-Id` a `X-Fresh-App-Secret`.

## 0.4.5 / Fresh Account Connector 0.1.4

- Admin objednávky AI Engine jsou načítány z Fresh Crackers a v Engine jsou pouze pro čtení.
- Stav objednávky se mění výhradně v e-shopu Fresh Crackers.
- Propojený účet lze z adminu Fresh AI Engine upravit; identita a fakturační údaje se bezpečně zapisují přes autorizované API do centrálního účtu Fresh Crackers.
- Hesla se přes API nikdy neposílají ani nevracejí.
