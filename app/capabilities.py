"""Transparent, deterministic text capability assignment (no external AI calls)."""
import json
import re
from sqlalchemy import select
from app.models import CapabilityDefinition

CODE_RE = re.compile(r'^[a-z][a-z0-9_]{1,49}$')
MEDIA_CAPABILITIES = {'image_generation', 'image_understanding', 'video_generation', 'video_understanding'}
DEFAULTS = (
    ('general', 'Obecná textová úloha', 'Výchozí typ, pokud volající aplikace neurčila schopnost a nenašlo se pravidlo.', 0, []),
    ('chat', 'Konverzace', 'Běžná konverzace a dialog.', 20, ['popovídej', 'povídej si', 'chat', 'konverzace']),
    ('analysis', 'Analýza', 'Analytické a srovnávací textové úlohy.', 55, ['analyzuj', 'analyze', 'rozbor', 'porovnej', 'compare', 'vyhodnoť', 'analýzu']),
    ('marketing', 'Marketing', 'Texty pro kampaně, sítě, e-maily a komunikaci.', 65, ['marketingový', 'marketing', 'reklamní text', 'příspěvek na facebook', 'příspěvek na instagram', 'newsletter', 'prodejní text']),
    ('seo', 'SEO', 'Úkoly optimalizace obsahu pro vyhledávání.', 75, ['seo', 'klíčová slova', 'meta description', 'vyhledávače', 'search engine optimization']),
    ('coding', 'Programování', 'Vývoj softwaru, debugování a tvorba kódu.', 90, ['napiš kód', 'napiš skript', 'programování', 'debug', 'oprav plugin', 'implementuj', 'javascript', 'python', 'php', 'sql', 'plugin', 'oprav kód', 'uprav kód', 'refaktoruj']),
    ('image_generation', 'Generování obrázků', 'Dostupné synchronně přes operation image.generate a přes /v1/media/image/generate.', 0, []),
    ('image_understanding', 'Čtení obrázků', 'Dostupné synchronně přes operation image.understand a přes /v1/media/image/understand.', 0, []),
    ('video_generation', 'Generování videí', 'Adaptér není připojen; úloha zatím není dostupná.', 0, []),
    ('video_understanding', 'Čtení videí', 'Adaptér není připojen; úloha zatím není dostupná.', 0, []),
)

def normalize_phrases(value):
    """Accept one phrase per line or a JSON string list; use predictable exact word/phrase rules."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            decoded = value.splitlines()
        value = decoded
    if not isinstance(value, list):
        raise ValueError('Zadej fráze po řádcích nebo jako JSON seznam.')
    phrases = []
    for part in value:
        if not isinstance(part, str):
            raise ValueError('Fráze musejí být textové.')
        phrase = ' '.join(part.strip().casefold().split())
        if phrase and phrase not in phrases:
            if len(phrase) > 100:
                raise ValueError('Fráze smí mít maximálně 100 znaků.')
            phrases.append(phrase)
    if len(phrases) > 30:
        raise ValueError('Maximálně 30 rozpoznávacích frází pro jednu schopnost.')
    return phrases

def definitions(db):
    return db.scalars(select(CapabilityDefinition).order_by(
        CapabilityDefinition.priority.desc(), CapabilityDefinition.code)).all()

def known_types(db):
    return {row.code for row in definitions(db)}

def match_phrase(prompt: str, phrase: str):
    return bool(re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', prompt, re.UNICODE))

def assign_task(db, prompt: str, requested: str | None):
    """Explicit API type wins. Otherwise highest-priority matching enabled text rule wins."""
    configured = definitions(db)
    by_code = {row.code: row for row in configured}
    if requested is not None and requested.strip():
        code = requested.strip().lower()
        row = by_code.get(code)
        return code, {'source': 'explicit_api', 'capability': code,
                      'definition': row.description if row else 'Typ dodaný volající aplikací; není v registru.',
                      'matched_rule': None, 'rule_priority': row.priority if row else None}
    normalized = ' '.join(prompt.casefold().split())
    matches = []
    for row in configured:
        if not row.enabled or row.code == 'general' or row.code in MEDIA_CAPABILITIES:
            continue
        for phrase in normalize_phrases(row.phrases_json):
            if match_phrase(normalized, phrase):
                matches.append((row.priority, len(phrase), row.code, phrase, row))
    if matches:
        priority, _, code, phrase, row = sorted(matches, key=lambda item: (-item[0], -item[1], item[2]))[0]
        return code, {'source': 'keyword_rule', 'capability': code,
                      'definition': row.description, 'matched_rule': phrase,
                      'rule_priority': priority,
                      'other_matched_capabilities': sorted({m[2] for m in matches if m[2] != code})}
    return 'general', {'source': 'default_general', 'capability': 'general',
                       'definition': (by_code['general'].description if 'general' in by_code else
                                      'Žádné pravidlo neodpovídalo zadání; použita obecná textová úloha.'),
                       'matched_rule': None, 'rule_priority': None}


def assign_with_context(db, prompt, requested, context=None):
    """Use previous user messages only for short follow-up to an identifiable task."""
    task, trace = assign_task(db, prompt, requested)
    if requested or task not in {'general', 'chat'}:
        return task, trace
    import re
    brief = ' '.join(str(prompt).casefold().split())
    if not re.match(r'^(?:pokračuj|pokracuj|udělej to|udelej to|oprav to|dokonči|dokonc|navaz|a co dál|a co dal)', brief):
        return task, trace
    previous = (context or {}).get('conversation') or []
    for row in reversed(previous[-8:]):
        if isinstance(row,dict) and row.get('role') == 'user':
            previous_task,_ = assign_task(db,str(row.get('content') or '')[:2000],None)
            if previous_task != 'general':
                return previous_task, {'source':'conversation_continuity', 'capability': previous_task,
                    'matched_rule':'last_user_task', 'rule_priority':None}
    return task, trace
