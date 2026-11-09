# Fresh AI Engine 0.7.0 — Fresh Key

## Co verze přidává

Fresh Key je společná identitní a projektová vrstva Fresh ekosystému. Nedrží pracovní data Lindy/Bobana/Edy/Karla; každá aplikace si dál vede vlastní databázi. Fresh Key drží pouze:

- globální Fresh identity a vazbu na lokální účty jednotlivých aplikací,
- globální projekty a členství/role,
- mapování lokálních projektů na jeden Fresh Project ID,
- projektové externí konektory,
- šifrované OAuth credentials a asset tokens,
- OAuth broker pro Meta, LinkedIn a Google Business Profile.

## Bezpečné nasazení

1. Zálohuj databázi AI Enginu a celý adresář aplikace.
2. Nahraj soubory této verze přes stávající instalaci, `.env` nepřepisuj.
3. Aktivuj virtualenv a spusť `pip install -r requirements.txt` (nově je potřeba `cryptography`).
4. Doplň do produkčního `.env` minimálně:

```env
FRESH_KEY_ENCRYPTION_KEY=<unikatni_nahodny_retezec_32+_znaku>
FRESH_KEY_PUBLIC_ORIGIN=https://aiengine.freshcrackers.cz
FRESH_KEY_ALLOWED_RETURN_HOSTS=.freshcrackers.cz
```

`FRESH_KEY_ENCRYPTION_KEY` po prvním produkčním použití neměň bez řízené migrace tokenů a bezpečně jej zálohuj. Slouží k šifrování OAuth access/refresh tokenů uložených ve Fresh Key.

5. Pro jednotlivé providery doplň Client ID/Secret až po vytvoření developer aplikací:

```env
FRESH_KEY_META_CLIENT_ID=
FRESH_KEY_META_CLIENT_SECRET=
FRESH_KEY_META_API_VERSION=v24.0
FRESH_KEY_META_SCOPES=pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish

FRESH_KEY_LINKEDIN_CLIENT_ID=
FRESH_KEY_LINKEDIN_CLIENT_SECRET=
FRESH_KEY_LINKEDIN_SCOPES=openid profile email w_member_social
FRESH_KEY_LINKEDIN_VERSION=202609

FRESH_KEY_GOOGLE_CLIENT_ID=
FRESH_KEY_GOOGLE_CLIENT_SECRET=
FRESH_KEY_GOOGLE_SCOPES=openid email profile https://www.googleapis.com/auth/business.manage
```

6. Restartuj Passenger aplikaci.

Při startu `Base.metadata.create_all()` bezpečně vytvoří pouze chybějící Fresh Key tabulky. Pokud DB uživatel nemá `CREATE TABLE`, použij `sql-0.7.0-fresh-key.sql` ručně v phpMyAdminu.


## Fresh Key SSO mezi aplikacemi

Linda a další Fresh aplikace mohou uživatele poslat na `https://aiengine.freshcrackers.cz/fresh-key/sso/authorize`. Engine ověří centrální Fresh účet a e-mail a vrátí aplikaci krátkodobý jednorázový kód. Aplikace jej server-to-server vymění a bezpečně naváže svůj lokální účet na globální Fresh Key identitu.

Důležité: samotná shoda textu e-mailu nestačí k převzetí identity ani pozvaného projektu. Neověřený lokální účet musí projít centrálním Fresh Key přihlášením. Pozvánka do projektu zůstává `pending`, dokud Fresh Key neověří identitu držitele e-mailu.

## OAuth callback URL

Do developer konzolí zaregistruj přesně:

- Meta: `https://aiengine.freshcrackers.cz/fresh-key/oauth/meta/callback`
- LinkedIn: `https://aiengine.freshcrackers.cz/fresh-key/oauth/linkedin/callback`
- Google: `https://aiengine.freshcrackers.cz/fresh-key/oauth/google_business/callback`

OAuth se vždy spouští z AI Enginu/Fresh Key, i když uživatel klikne na tlačítko v Lindě. Linda nedostává provider Client Secret a ukládá jen bezpečný pointer na `fresh_key_connection_id`.

## Poznámky k providerům

- Meta: jeden login umí nabídnout dostupné Facebook Pages a navázané Instagram professional účty; uživatel assety ještě vybere ve Fresh Key.
- LinkedIn: výchozí konfigurace je připravena pro osobní profil. Firemní LinkedIn Pages se zobrazí až po schválení odpovídajících organization/Community Management oprávnění a jejich přidání do `FRESH_KEY_LINKEDIN_SCOPES`.
- Google Business Profile: OAuth je připraven, ale Google Cloud projekt musí mít povolený příslušný Business Profile API přístup.

## Fresh Key vs. API konektory

Původní AI Engine `connectors` zůstávají beze změny — jsou to aplikační Bearer klíče pro Lindu/Bobana/Chat. Fresh Key používá vlastní tabulky `fresh_key_*`; tyto dva typy konektorů se neslučují.

## Pozvání člena projektu

Členy lze spravovat v `/account/fresh-key` i přes API konkrétní Fresh aplikace. Pozvánka na dosud neověřený e-mail zůstane `pending` a aktivuje se až po ověření centrální Fresh Key identity.

## Vazba aplikačního klíče

API klíč aplikace se při prvním Fresh Key požadavku s hlavičkou `X-Fresh-App` naváže na daný `app_code`. Další požadavky stejného klíče už nemohou přepnout na namespace jiné Fresh aplikace. Linda hlavičku posílá automaticky jako `linda`.
