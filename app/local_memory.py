"""Deterministic, evidence-bound private memory lookup. No external provider calls."""
import re
import unicodedata

QUERY = re.compile(r"^\s*(?:co\s+si\s+(?:o\s+.{2,120}\s+)?pamatujes|co\s+vis\s+o|co\s+jsem\s+ti\s+rekl\s+o|pripomen\s+mi\s+co\s+vis\s+o)\b", re.I)
STOP = {'jsem','jsme','jsi','bylo','byla','byly','mame','mate','moje','muj','nase','tobe','tebe','jeste','jake','jaky','jakou','jakem','tento','tomto','ktere','kterou','ktery','prosím','prosim','pamatujes','rekl','vis','pripomen','zapis','zapamatuj','mas','ulozene'}

def normalize(text):
    text = unicodedata.normalize('NFKD', str(text)).casefold()
    return ''.join(ch for ch in text if not unicodedata.combining(ch))

def terms(text):
    return set(t for t in re.findall(r'[a-z0-9]{4,}', normalize(text)) if t not in STOP)

def local_answer(prompt, context):
    """Returns (answer, metadata) or None. `context` has been authenticated by caller."""
    receipt = context.get('memory_operation')
    if isinstance(receipt, dict) and receipt.get('action') == 'stored' and str(receipt.get('id','')).isdigit():
        return ('Uložil jsem tuto poznámku do tvé soukromé paměti. V dalších chatech ji mohu dohledat.',
                {'reason': 'user_confirmed_note_saved', 'memory_id': str(receipt['id'])})
    if not QUERY.search(normalize(prompt)):
        return None
    query_terms = terms(prompt)
    if not query_terms:
        return None
    candidates = context.get('memory_candidates') or []
    supported = []
    for item in candidates[:8]:
        if not isinstance(item, dict) or item.get('kind') != 'explicit_user_note':
            continue
        note = str(item.get('text') or '').strip()
        common = terms(note) & query_terms
        if note and common and len(note) <= 1800:
            supported.append((len(common), item))
    if not supported:
        return None  # No unsupported 'I remember nothing' statement.
    supported.sort(key=lambda entry: entry[0], reverse=True)
    # Only repeat original, owner-scoped notes; do not infer a new factual answer.
    notes = [item for _, item in supported[:3]]
    output = 'V tvých uložených poznámkách jsem našel:\n' + '\n'.join('• „' + str(item['text']) + '“' for item in notes)
    return (output, {'reason': 'explicit_user_note_exact_quote', 'memory_ids': [str(n['id']) for n in notes]})
