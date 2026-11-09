"""Admin cost analytics for Fresh AI Engine.

The report separates application economics from provider-call economics. Provider
spend is reconstructed primarily from JobAttempt rows so known costs of invalid
outputs/retries are not silently lost. Historical completed jobs without attempts
fall back to the booked Job cost. Values remain estimates based on configured
model tariffs, not provider invoices.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select

from app.models import ChatUsageUser, CustomerAccount, Job, JobAttempt

PERIODS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
    "custom": None,
}
PRAGUE = ZoneInfo("Europe/Prague")


def _utc_boundary(raw: str | None, end: bool = False) -> datetime | None:
    if not raw:
        return None
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None
    local = datetime.combine(d, time.max if end else time.min, tzinfo=PRAGUE)
    return local.astimezone(timezone.utc)


def _user_labels(db, user_ids: list[str]) -> dict[str, str]:
    ids = [x for x in set(user_ids) if x]
    labels: dict[str, str] = {}
    if not ids:
        return labels
    account_ids = [x for x in ids if not x.startswith("chat:")]
    if account_ids:
        for row in db.scalars(select(CustomerAccount).where(CustomerAccount.id.in_(account_ids))).all():
            labels[row.id] = row.email
    chat_ids = [x for x in ids if x.startswith("chat:")]
    if chat_ids:
        for row in db.scalars(select(ChatUsageUser).where(ChatUsageUser.chat_user_id.in_(chat_ids))).all():
            label = row.display_name or row.email or row.chat_user_id
            if row.email and row.display_name:
                label += " · " + row.email
            labels[row.chat_user_id] = label
    for uid in ids:
        labels.setdefault(uid, uid)
    return labels


def _filters(period: str, source: str, project: str | None, user_id: str,
             provider: str, status: str, date_from: str, date_to: str):
    if period not in PERIODS:
        period = "30d"
    filters = []
    if period == "custom" or date_from or date_to:
        since = _utc_boundary(date_from, False)
        until = _utc_boundary(date_to, True)
        if since:
            filters.append(Job.created_at >= since)
        if until:
            filters.append(Job.created_at <= until)
        period = "custom"
    elif PERIODS[period] is not None:
        filters.append(Job.created_at >= datetime.now(timezone.utc) - PERIODS[period])
    if source:
        filters.append(Job.source == source)
    if project is not None:
        filters.append(Job.project_id.is_(None) if project == "" else Job.project_id == project)
    if user_id:
        filters.append(Job.user_id == user_id)
    if provider:
        filters.append(Job.provider == provider)
    if status:
        filters.append(Job.status == status)
    return period, filters


def cost_report(db, period="30d", source="", project=None, user_id="", provider="",
                status="", date_from="", date_to=""):
    period, filters = _filters(period, source, project, user_id, provider, status, date_from, date_to)

    sources = db.scalars(select(Job.source).distinct().order_by(Job.source)).all()
    projects = db.scalars(select(Job.project_id).where(Job.project_id.is_not(None)).distinct().order_by(Job.project_id)).all()
    providers_filter = db.scalars(select(Job.provider).where(Job.provider.is_not(None)).distinct().order_by(Job.provider)).all()
    statuses_filter = db.scalars(select(Job.status).distinct().order_by(Job.status)).all()
    user_ids = db.scalars(select(Job.user_id).where(Job.user_id.is_not(None)).distinct().order_by(Job.user_id)).all()
    user_labels = _user_labels(db, list(user_ids))
    users = [{"id": uid, "label": user_labels.get(uid, uid)} for uid in user_ids]

    statuses = dict(db.execute(
        select(Job.status, func.count(Job.id)).where(*filters).group_by(Job.status)
    ).all())

    # Job-level operational counts/tokens. Cost is added separately from attempts.
    job_groups = db.execute(
        select(Job.source, Job.project_id, Job.user_id,
               func.count(Job.id),
               func.sum(Job.input_tokens), func.sum(Job.output_tokens),
               func.sum(case((Job.status == "completed", 1), else_=0)))
        .where(*filters)
        .group_by(Job.source, Job.project_id, Job.user_id)
    ).all()

    apps = {}
    totals = {"jobs": 0, "completed": 0, "cost": 0.0, "booked_success": 0.0,
              "input": 0, "output": 0, "users": set(), "internal_cost": 0.0}
    for src, prj, uid, count, in_tokens, out_tokens, completed in job_groups:
        key = (src, prj, uid)
        row = apps.setdefault(key, {"source": src, "project": prj, "user_id": uid,
                                    "user_label": user_labels.get(uid, uid) if uid else "Bez uživatele",
                                    "jobs": 0, "completed": 0, "failed": 0,
                                    "cost": 0.0, "input": 0, "output": 0, "providers": {}})
        row["jobs"] += int(count or 0)
        row["completed"] += int(completed or 0)
        row["input"] += int(in_tokens or 0)
        row["output"] += int(out_tokens or 0)
        row["failed"] += int(count or 0) - int(completed or 0)
        totals["jobs"] += int(count or 0)
        totals["completed"] += int(completed or 0)
        totals["input"] += int(in_tokens or 0)
        totals["output"] += int(out_tokens or 0)
        if uid:
            totals["users"].add(uid)

    # Provider calls with a known token-based cost. This includes invalid structured
    # outputs and any historical multi-call jobs, which provider billing may still charge.
    attempt_rows = db.execute(
        select(Job.source, Job.project_id, Job.user_id, JobAttempt.provider,
               func.count(JobAttempt.id), func.sum(JobAttempt.estimated_cost_usd),
               func.sum(JobAttempt.input_tokens), func.sum(JobAttempt.output_tokens))
        .join(Job, Job.id == JobAttempt.job_id)
        .where(*filters)
        .group_by(Job.source, Job.project_id, Job.user_id, JobAttempt.provider)
    ).all()
    provider_map = {}
    known_attempt_cost = 0.0
    for src, prj, uid, prov, calls, cost, in_tokens, out_tokens in attempt_rows:
        cost = float(cost or 0)
        known_attempt_cost += cost
        key = (src, prj, uid)
        row = apps.setdefault(key, {"source": src, "project": prj, "user_id": uid,
                                    "user_label": user_labels.get(uid, uid) if uid else "Bez uživatele",
                                    "jobs": 0, "completed": 0, "failed": 0,
                                    "cost": 0.0, "input": 0, "output": 0, "providers": {}})
        row["cost"] += cost
        row["providers"][prov or "nezjištěno"] = row["providers"].get(prov or "nezjištěno", 0.0) + cost
        p = provider_map.setdefault(prov or "nezjištěno", {"provider": prov or "nezjištěno", "calls": 0,
                                                            "cost": 0.0, "input": 0, "output": 0})
        p["calls"] += int(calls or 0)
        p["cost"] += cost
        p["input"] += int(in_tokens or 0)
        p["output"] += int(out_tokens or 0)

    # Legacy fallback: completed jobs created before attempt tracking. Do not add
    # the job-level cost when any attempt exists, otherwise we'd double count.
    legacy_rows = db.execute(
        select(Job.source, Job.project_id, Job.user_id, Job.provider,
               func.count(Job.id), func.sum(Job.estimated_cost_usd))
        .where(*filters, Job.status == "completed",
               ~select(JobAttempt.id).where(JobAttempt.job_id == Job.id).exists())
        .group_by(Job.source, Job.project_id, Job.user_id, Job.provider)
    ).all()
    legacy_cost = 0.0
    for src, prj, uid, prov, count, cost in legacy_rows:
        cost = float(cost or 0)
        legacy_cost += cost
        key = (src, prj, uid)
        row = apps.setdefault(key, {"source": src, "project": prj, "user_id": uid,
                                    "user_label": user_labels.get(uid, uid) if uid else "Bez uživatele",
                                    "jobs": 0, "completed": 0, "failed": 0,
                                    "cost": 0.0, "input": 0, "output": 0, "providers": {}})
        row["cost"] += cost
        row["providers"][prov or "nezjištěno"] = row["providers"].get(prov or "nezjištěno", 0.0) + cost
        p = provider_map.setdefault(prov or "nezjištěno", {"provider": prov or "nezjištěno", "calls": 0,
                                                            "cost": 0.0, "input": 0, "output": 0})
        p["calls"] += int(count or 0)
        p["cost"] += cost

    totals["cost"] = known_attempt_cost + legacy_cost
    totals["booked_success"] = float(db.scalar(
        select(func.coalesce(func.sum(Job.estimated_cost_usd), 0.0)).where(*filters, Job.status == "completed")
    ) or 0.0)
    totals["internal_cost"] = sum(row["cost"] for row in apps.values() if (row["source"] or "") == "admin-console")
    totals["user_count"] = len(totals.pop("users"))

    failures_unknown = db.scalar(
        select(func.count(JobAttempt.id)).join(Job, Job.id == JobAttempt.job_id)
        .where(*filters, JobAttempt.status == "failed", JobAttempt.estimated_cost_usd <= 0)
    ) or 0
    failures_known = db.scalar(
        select(func.coalesce(func.sum(JobAttempt.estimated_cost_usd), 0.0)).join(Job, Job.id == JobAttempt.job_id)
        .where(*filters, JobAttempt.status.in_(["failed", "invalid_output"]), JobAttempt.estimated_cost_usd > 0)
    ) or 0.0
    zero_cost_calls = db.scalar(
        select(func.count(JobAttempt.id)).join(Job, Job.id == JobAttempt.job_id)
        .where(*filters, JobAttempt.status.in_(["success", "invalid_output"]), JobAttempt.estimated_cost_usd == 0,
               JobAttempt.provider != "mock")
    ) or 0

    recent = db.scalars(select(Job).where(*filters).order_by(Job.created_at.desc()).limit(50)).all()
    app_rows = sorted(apps.values(), key=lambda row: (-row["cost"], row["source"] or "", row["project"] or "", row["user_label"] or ""))
    provider_rows = sorted(provider_map.values(), key=lambda row: (-row["cost"], row["provider"]))
    return {
        "period": period, "source": source, "project": project, "user_id": user_id,
        "provider": provider, "status": status, "date_from": date_from, "date_to": date_to,
        "sources": sources, "projects": projects, "providers_filter": providers_filter,
        "statuses_filter": statuses_filter, "users": users, "user_labels": user_labels,
        "apps": app_rows, "providers": provider_rows, "totals": totals,
        "statuses": statuses, "failed_attempts_unknown": int(failures_unknown),
        "failed_attempts_known_cost": float(failures_known), "zero_cost_calls": int(zero_cost_calls),
        "recent": recent,
    }
