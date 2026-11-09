"""Indicative latest official CNB USD/CZK fixing; cached in Engine DB.
Refresh at most twice daily (normally via existing queue cron). Fail closed to
last known rate and show its date; never invent a fallback exchange rate.
"""
from datetime import datetime, timedelta, timezone
import httpx
from app.models import RuntimeSetting
from app.accounts import credit_setting

CNB_URL = ('https://www.cnb.cz/cs/financni-trhy/devizovy-trh/'
           'kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt')


def parse_cnb_usd(body):
    lines = body.strip().splitlines()
    if not lines:
        raise ValueError('Prázdná odpověď ČNB')
    first = lines[0].strip().split(' #')[0]
    try:
        rate_date = datetime.strptime(first, '%d.%m.%Y').date()
    except ValueError as exc:
        raise ValueError('Neplatné datum kurzu ČNB') from exc
    today = datetime.now(timezone.utc).date()
    if rate_date > today or (today - rate_date).days > 10:
        raise ValueError('Datum kurzu ČNB je mimo očekávané období')
    rates = [line.split('|') for line in lines[2:] if '|USD|' in line]
    for row in rates:
        if len(row) == 5 and row[3].strip() == 'USD':
            quantity = float(row[2].replace(',', '.'))
            value = float(row[4].replace(',', '.'))
            if quantity > 0 and 1 < value / quantity < 1000:
                return value / quantity, rate_date.isoformat()
    raise ValueError('V exportu ČNB nebyl nalezen platný kurz USD')


def _set(db, key, val):
    row = db.get(RuntimeSetting, key)
    if row:
        row.value = str(val)
    else:
        db.add(RuntimeSetting(key=key, value=str(val)))


def refresh_cnb_fx(db, force=False, fetcher=None):
    """Returns (rate, date, state). On network failures retains last valid rate."""
    now = datetime.now(timezone.utc)
    checked = db.get(RuntimeSetting, 'cnb_usd_checked_at')
    last = None
    if checked:
        try:
            last = datetime.fromisoformat(checked.value)
            if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if not force and last and now - last < timedelta(hours=12):
        return fx_status(db)
    # Save attempt time to prevent excessive retry traffic on repeated failures.
    _set(db, 'cnb_usd_checked_at', now.isoformat())
    db.commit()
    try:
        if fetcher is None:
            with httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0), follow_redirects=False) as client:
                response = client.get(CNB_URL, headers={'Accept':'text/plain'})
                response.raise_for_status()
                body = response.text
        else:
            body = fetcher()
        value, published = parse_cnb_usd(body)
        _set(db, 'usd_czk_reporting_rate', value)
        _set(db, 'cnb_usd_published', published)
        _set(db, 'cnb_usd_status', 'ok')
        db.commit()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        db.rollback()
        _set(db, 'cnb_usd_status', 'unavailable')
        db.commit()
    return fx_status(db)


def fx_status(db):
    date = db.get(RuntimeSetting, 'cnb_usd_published')
    status = db.get(RuntimeSetting, 'cnb_usd_status')
    rate = credit_setting(db, 'usd_czk_reporting_rate', 0.0)
    return dict(rate=rate if rate > 0 else 0.0,
                published=date.value if date else '',
                state=status.value if status else 'not_loaded',
                source=('ČNB – kurz devizového trhu (střed)' if date else 'Dřívější neověřený kurz'), url=CNB_URL)
