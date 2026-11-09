# Fresh AI Engine 0.7.4.2 – Fresh Key owner hotfix

Opravuje falešné 403 „K tomuto Fresh Key propojení nemáš potřebné oprávnění“ při prvním odeslání projektu z aplikace.

Příčina: `SessionLocal` používá `autoflush=False`; nově vytvořený owner membership nebyl před interní kontrolou zapsán do DB. Verze 0.7.4.2 členství explicitně flushne a umí bezpečně opravit chybějící membership pouze u jednoznačného vlastníka propojení.

Žádná SQL migrace. Zachovej produkční `.env`.
