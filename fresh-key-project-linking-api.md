# Fresh Key 0.7.4 — API pro bezplatné potvrzované Propojení

## Princip

Fresh Key je bezplatná spojovací a identitní vrstva Fresh ekosystému. Nevyžaduje kredity a nevytváří pracovní projekty. Každá aplikace vlastní svůj lokální projekt, pracovní data, lidi a role. Fresh Key pouze drží společné ID, vazby mezi lokálními projekty, přehled členství a projektové externí konektory.

Názvy lokálních projektů se nemusí shodovat. `CajnaCars.cz` v Lindě, `ČajnaCars.cz` v Bobanovi a `Čajna Cars` v Edovi mohou být jeden společný projekt, pokud to uživatel explicitně potvrdí.

## 0. Bezplatná identita / AI Engine účet

`POST /v1/fresh-key/account/bootstrap`

Důvěryhodná Fresh aplikace může při přihlášení nebo otevření bloku **Propojení** připravit uživateli bezplatnou identitu Fresh Key a lehký účet AI Enginu. Tento krok:

- neodebírá kredity,
- nevyžaduje nákup,
- neodesílá žádný projekt do Fresh Key,
- umožní tlačítko **Spravovat propojení** směrovat uživatele do AI Enginu.

Příklad těla:

```json
{
  "app_code": "linda",
  "external_user_id": "123",
  "email": "uzivatel@example.cz",
  "display_name": "Uživatel",
  "email_verified": true
}
```

Odpověď obsahuje mimo jiné `identity_id`, `account_id`, `manage_url`, `login_url`, `free: true` a `credits_required: false`.

## 1. Inventář lokálního projektu

`POST /v1/fresh-key/local-projects/sync`

Aplikace nahlásí svůj existující projekt pouze v rámci explicitního workflow (např. **Odeslat do Fresh Key**, přijetí návrhu propojení nebo vytvoření cílového projektu po potvrzení). Běžné otevření stránky projektu ani jeho lokální vytvoření nesmí tento endpoint volat automaticky.

```json
{
  "external_user_id": "123",
  "external_project_id": "17",
  "name": "Fresh Crackers",
  "website_url": "https://www.freshcrackers.cz",
  "organization_name": "Fresh Online s.r.o.",
  "role": "owner"
}
```

## 2. Odeslat zdrojový projekt do Fresh Key

`POST /v1/fresh-key/links/sync`

Aplikace tento endpoint volá až po explicitním kliknutí uživatele na **Odeslat do Fresh Key**. Pokud projekt ještě není propojený, vznikne jeden Fresh Key link obsahující jen zdrojovou aplikaci.

Pro odeslání je lokálně povolen pouze **Vlastník** nebo **Správce** projektu.

## 3. AI Engine: Připojit další App

AI Engine zobrazuje každý Fresh Key link jako jeden řádek. Uživatel vybere cílovou aplikaci a:

- existující, dosud nepropojený projekt cílové aplikace, nebo
- **Vytvořit nový propojený projekt**.

Engine vytvoří `FreshKeyLinkRequest` ve stavu `pending`. Nic se ještě nespojí ani nevytvoří. Cílová aplikace dostane návrh a může zobrazit upozornění i e-mail s potvrzením.

## 4. Cílová aplikace potvrzuje

Čekající návrhy:

`GET /v1/fresh-key/link-requests?status=pending`

Potvrzení existujícího projektu:

`POST /v1/fresh-key/link-requests/{id}/accept`

Cílová aplikace posílá vlastní `external_project_id`, název, URL a lokální roli. Teprve po tomto potvrzení Fresh Key vytvoří vazbu.

U návrhu `create_new` cílová aplikace nejdřív lokálně vytvoří pracovní projekt a až potom stejným `/accept` endpointem vrátí jeho lokální ID.

Odmítnutí:

`POST /v1/fresh-key/link-requests/{id}/decline`

## 5. Role a vztah k projektu

Oprávnění jsou jednotná:

- `owner` — Vlastník: vše včetně sdílení a Fresh Key propojení,
- `manager` — Správce: editace, schvalování, sdílení a Fresh Key propojení,
- `editor` — Editor: práce, editace a schvalování; bez správy lidí a propojení,
- `viewer` — Čtenář: pouze čtení.

`client`, `agency` a `member` jsou vztahy k projektu, nikoliv oprávnění. Aplikace si je drží lokálně jako metadata (Klient / Agentura / Člen týmu).

## 6. Lidé jsou autoritativní v aplikaci

Lokální aplikace může zrcadlit lidi pro read-only přehled AI Enginu přes:

`POST /v1/fresh-key/links/{fresh_key_id}/members`

Tělo vždy obsahuje vlastní `app_code`/lokálního uživatele. Fresh Key si zrcadlo ukládá **po aplikacích**, takže člověk může být např. Editor v Lindě a Čtenář v Bobanovi. Odebrání z jedné aplikace ho neodstraní z přehledu, pokud má přístup ještě v jiné.

Tato data **neudělují lokální přístup**. Lokální `project_members`, `ProjectMember`, role a ACL jsou vždy autoritou konkrétní aplikace.

Pro e-mail potvrzení/pozvánky ke sdílení lze použít:

`POST /v1/fresh-key/project-share/notify`

Ani tento endpoint žádný lokální přístup nevytváří.

## 7. Spravovat propojení

Každá aplikace má vedle **Odeslat do Fresh Key** také **Spravovat propojení**, které vede do centrálního přehledu AI Enginu. Aplikace ale zůstává autoritou pro svůj lokální projekt, role a pracovní data.

## 8. Identita uživatele

API klíč identifikuje Fresh aplikaci a `external_user_id` se mapuje přes Fresh Key identity binding. Dlouhodobě se tím používá jeden Fresh User/Fresh Key účet napříč aplikacemi, místo porovnávání názvů projektů nebo slepého propojování pouze podle e-mailu.


## 0.8 duplicate-safe publish
Apps mohou u `/v1/fresh-key/links/sync` poslat `linking_version: 2` nebo `resolve_duplicates: true`. Pokud Fresh Key najde existující kandidát podle normalizované URL (fallback přes normalizovaný název), vrátí `status=match_required`, `candidates[]` a `manage_url` a nevytvoří druhé Propojení. Starší aplikace bez tohoto příznaku zůstávají kompatibilní s původním chováním.
