"""24h read-only diagnostic snapshot shared by a high-entropy revocable bearer link."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from app.models import DiagnosticShare
from app.diagnostics import report, support_bundle

MAX_BUNDLE_BYTES = 12 * 1024 * 1024

def utcnow():
    return datetime.now(timezone.utc)

def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt

def current(db):
    row = db.get(DiagnosticShare, 'current')
    if row and _aware(row.expires_at) > utcnow():
        return row
    return None

def authenticated_share(db, token):
    """No access if missing/revoked/expired; constant-time digest compare."""
    row = current(db)
    if not row or not isinstance(token, str) or len(token) > 120:
        return None
    if not secrets.compare_digest(row.token_hash, _hash(token)):
        return None
    return row

def snapshot(db):
    # Generate both resources before changing the stored snapshot: failed refresh
    # never corrupts an existing working link.
    manifest = report(db)
    data = json.dumps(manifest, ensure_ascii=False, indent=2)
    bundle = support_bundle(db, manifest=manifest).getvalue()
    if len(bundle) > MAX_BUNDLE_BYTES:
        raise ValueError('Diagnostický ZIP přesahuje povolenou velikost 12 MiB.')
    return data, bundle

def generate(db):
    data, bundle = snapshot(db)
    token = secrets.token_urlsafe(32)
    now = utcnow()
    row = db.get(DiagnosticShare, 'current')
    if row is None:
        row = DiagnosticShare(id='current', token_hash=_hash(token),
                              issued_at=now, expires_at=now+timedelta(hours=24),
                              refreshed_at=now, manifest_json=data, bundle_zip=bundle)
        db.add(row)
    else:
        row.token_hash = _hash(token)
        row.issued_at = now
        row.expires_at = now + timedelta(hours=24)
        row.refreshed_at = now
        row.manifest_json = data
        row.bundle_zip = bundle
    db.commit()
    return token, row

def refresh(db):
    row = current(db)
    if not row:
        return None
    data, bundle = snapshot(db)
    row.manifest_json = data
    row.bundle_zip = bundle
    row.refreshed_at = utcnow()
    db.commit()
    return row

def revoke(db):
    row = db.get(DiagnosticShare, 'current')
    if row:
        db.delete(row)
        db.commit()
