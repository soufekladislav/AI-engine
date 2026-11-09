from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

try:
    import httpx
except Exception:  # pragma: no cover - runtime dependency is already required by providers
    httpx = None

from app.models import RuntimeSetting


@dataclass
class ProviderFailure:
    provider: str
    category: str
    status_code: int | None
    retryable: bool
    account_level: bool
    cooldown_seconds: int
    public_message: str
    technical_detail: str


_BILLING_PATTERNS = (
    "credit balance", "insufficient credit", "insufficient balance", "purchase credits",
    "billing", "payment required", "payment_required", "account balance", "out of credits",
    "quota exhausted", "quota_exhausted", "spending limit", "usage limit",
)
_AUTH_PATTERNS = ("invalid api key", "authentication", "unauthorized", "forbidden", "permission denied")
_RATE_PATTERNS = ("rate limit", "rate_limit", "too many requests", "overloaded")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Do not persist bearer-like tokens if a provider ever echoes them.
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(api[-_ ]?key[\"':= ]+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    return text[:limit]


def classify_provider_exception(provider: str, exc: Exception) -> ProviderFailure:
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "public_detail", None) or str(exc)
    retry_after = getattr(exc, "retry_after_seconds", None)

    # httpx exceptions raised by legacy adapters still carry response/request metadata.
    if status is None and httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        try:
            status = int(exc.response.status_code)
            detail = exc.response.text or detail
            retry_after_raw = exc.response.headers.get("retry-after")
            if retry_after_raw and str(retry_after_raw).isdigit():
                retry_after = int(retry_after_raw)
        except Exception:
            pass

    low = str(detail or "").lower()
    is_timeout = exc.__class__.__name__.lower().find("timeout") >= 0
    is_network = exc.__class__.__name__.lower() in {
        "connecterror", "networkerror", "readerror", "writeerror", "remoteprotocolerror"
    }

    if status == 402 or any(p in low for p in _BILLING_PATTERNS):
        category, retryable, account_level, cooldown = "billing_or_credit", True, True, 7200
        public = "Provider nemá dostupný kredit / billing a byl dočasně vyřazen z routingu."
    elif status in {401, 403} or any(p in low for p in _AUTH_PATTERNS):
        category, retryable, account_level, cooldown = "authentication", True, True, 21600
        public = "Provider odmítl autorizaci a byl dočasně vyřazen z routingu."
    elif status == 429 or any(p in low for p in _RATE_PATTERNS):
        category, retryable, account_level = "rate_limit", True, False
        cooldown = max(60, min(int(retry_after or 300), 3600))
        public = "Provider je dočasně omezen limitem požadavků; Engine použije jinou platformu."
    elif status is not None and int(status) >= 500:
        category, retryable, account_level, cooldown = "provider_unavailable", True, False, 180
        public = "Provider je dočasně nedostupný; Engine použije jinou platformu."
    elif is_timeout or is_network:
        category, retryable, account_level, cooldown = "network_or_timeout", True, False, 120
        public = "Spojení s providerem selhalo; Engine použije jinou platformu."
    else:
        category, retryable, account_level, cooldown = "provider_error", True, False, 300
        public = "Provider vrátil technickou chybu; Engine použije jinou platformu."

    return ProviderFailure(
        provider=provider,
        category=category,
        status_code=int(status) if status is not None else None,
        retryable=retryable,
        account_level=account_level,
        cooldown_seconds=cooldown,
        public_message=public,
        technical_detail=_safe_text(detail),
    )


def _key(provider: str) -> str:
    return f"provider_guard:{provider}"[:100]


def provider_guard_state(db, provider: str) -> dict:
    row = db.get(RuntimeSetting, _key(provider))
    if row is None or not row.value:
        return {"state": "closed", "provider": provider}
    try:
        data = json.loads(row.value)
    except Exception:
        return {"state": "closed", "provider": provider}
    until_raw = data.get("blocked_until")
    if not until_raw:
        return {"state": "closed", "provider": provider}
    try:
        until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        now = utcnow()
        if until > now:
            remaining = int((until - now).total_seconds())
            return {
                "state": "open",
                "provider": provider,
                "category": data.get("category"),
                "blocked_until": until.isoformat(),
                "blocked_until_prague": until.astimezone(ZoneInfo("Europe/Prague")).isoformat(),
                "remaining_seconds": max(0, remaining),
                "status_code": data.get("status_code"),
                "failure_count": int(data.get("failure_count") or 1),
                "last_failure_at": data.get("last_failure_at"),
            }
        return {
            "state": "half_open",
            "provider": provider,
            "category": data.get("category"),
            "blocked_until": until.isoformat(),
            "blocked_until_prague": until.astimezone(ZoneInfo("Europe/Prague")).isoformat(),
            "failure_count": int(data.get("failure_count") or 1),
            "last_failure_at": data.get("last_failure_at"),
        }
    except Exception:
        return {"state": "closed", "provider": provider}


def mark_provider_failure(db, failure: ProviderFailure) -> dict:
    now = utcnow()
    existing = provider_guard_state(db, failure.provider)
    count = int(existing.get("failure_count") or 0) + 1
    # Repeated account-level failures get a longer cooldown, capped at 24h.
    cooldown = int(failure.cooldown_seconds)
    if failure.account_level and count > 1:
        cooldown = min(86400, cooldown * min(count, 4))
    blocked_until = now + timedelta(seconds=cooldown)
    payload = {
        "provider": failure.provider,
        "category": failure.category,
        "status_code": failure.status_code,
        "account_level": failure.account_level,
        "failure_count": count,
        "last_failure_at": now.isoformat(),
        "blocked_until": blocked_until.isoformat(),
        "public_message": failure.public_message,
        "technical_detail": failure.technical_detail[:500],
    }
    row = db.get(RuntimeSetting, _key(failure.provider))
    if row is None:
        row = RuntimeSetting(key=_key(failure.provider), value=json.dumps(payload, ensure_ascii=False))
        db.add(row)
    else:
        row.value = json.dumps(payload, ensure_ascii=False)
    return {"state": "open", **payload, "remaining_seconds": cooldown}


def clear_provider_guard(db, provider: str) -> bool:
    row = db.get(RuntimeSetting, _key(provider))
    if row is None:
        return False
    db.delete(row)
    return True


def all_provider_guards(db, providers: list[str] | tuple[str, ...]) -> dict[str, dict]:
    return {name: provider_guard_state(db, name) for name in providers}
