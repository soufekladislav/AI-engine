from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.models import RuntimeSetting

ALERTS_KEY = "admin_alerts_json"
MAX_ALERTS = 40


def utcnow():
    return datetime.now(timezone.utc)


def _load(row) -> list[dict]:
    if row is None or not row.value:
        return []
    try:
        value = json.loads(row.value)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def list_admin_alerts(db, include_dismissed: bool = False) -> list[dict]:
    row = db.get(RuntimeSetting, ALERTS_KEY)
    alerts = _load(row)
    if include_dismissed:
        return alerts
    return [a for a in alerts if not a.get("dismissed")]


def add_admin_alert(db, *, alert_type: str, severity: str, title: str, message: str,
                    dedupe_key: str | None = None, metadata: dict | None = None) -> dict:
    row = db.get(RuntimeSetting, ALERTS_KEY)
    alerts = _load(row)
    now = utcnow()
    # Same operational incident should not flood the console every few seconds.
    if dedupe_key:
        for existing in alerts[:15]:
            if existing.get("dedupe_key") != dedupe_key or existing.get("dismissed"):
                continue
            try:
                created = datetime.fromisoformat(str(existing.get("created_at")).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if (now - created).total_seconds() < 900:
                    existing["repeat_count"] = int(existing.get("repeat_count") or 1) + 1
                    existing["updated_at"] = now.isoformat()
                    existing["message"] = message[:1200]
                    if metadata:
                        existing["metadata"] = metadata
                    break
            except Exception:
                pass
        else:
            existing = None
        if existing is not None and existing.get("dedupe_key") == dedupe_key:
            pass
        else:
            alerts.insert(0, _new_alert(alert_type, severity, title, message, dedupe_key, metadata, now))
    else:
        alerts.insert(0, _new_alert(alert_type, severity, title, message, None, metadata, now))
    alerts = alerts[:MAX_ALERTS]
    payload = json.dumps(alerts, ensure_ascii=False, separators=(",", ":"))
    if row is None:
        db.add(RuntimeSetting(key=ALERTS_KEY, value=payload))
    else:
        row.value = payload
    return alerts[0] if alerts else {}


def _new_alert(alert_type, severity, title, message, dedupe_key, metadata, now):
    return {
        "id": uuid.uuid4().hex[:12],
        "type": alert_type[:60],
        "severity": severity[:20],
        "title": title[:180],
        "message": message[:1200],
        "dedupe_key": (dedupe_key or "")[:180] or None,
        "metadata": metadata or {},
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "repeat_count": 1,
        "dismissed": False,
    }


def dismiss_admin_alert(db, alert_id: str) -> bool:
    row = db.get(RuntimeSetting, ALERTS_KEY)
    alerts = _load(row)
    changed = False
    for alert in alerts:
        if alert.get("id") == alert_id:
            alert["dismissed"] = True
            alert["updated_at"] = utcnow().isoformat()
            changed = True
            break
    if changed and row is not None:
        row.value = json.dumps(alerts[:MAX_ALERTS], ensure_ascii=False, separators=(",", ":"))
    return changed


def clear_admin_alerts(db) -> None:
    row = db.get(RuntimeSetting, ALERTS_KEY)
    if row is not None:
        row.value = "[]"
