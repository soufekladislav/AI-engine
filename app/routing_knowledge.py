"""Deterministic routing knowledge for Fresh AI Engine Router 2.3.

The router never calls an LLM to choose another LLM. It combines:
- declared task/operation requirements,
- editable per-route model profiles,
- observed success/latency/cost from Fresh AI Engine's own jobs,
- requested quality/cost mode,
then selects the primary route before any provider call is made. Execution may fail over to another provider only after a real technical/provider failure or invalid required output.

Default profiles are Fresh AI Engine configuration hints, not vendor benchmarks.
They can be overridden per route in the admin UI.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models import Job, JobAttempt, JobFeedback

TEXT_TASKS = {"general", "chat", "analysis", "marketing", "seo", "coding"}
SKILLS = {
    "text_generation", "structured_json", "reasoning", "analysis", "coding",
    "seo", "marketing", "chat", "multimodal", "long_context", "speed", "reliability",
}

# Initial local routing knowledge. These numbers are operator hints used only
# until route-specific overrides and Fresh AI Engine's own history provide
# stronger evidence. They intentionally stay coarse (1..5).
DEFAULT_PROVIDER_PROFILES: dict[str, dict[str, int]] = {
    "openai": {
        "text_generation": 5, "structured_json": 5, "reasoning": 4,
        "analysis": 4, "coding": 4, "seo": 4, "marketing": 4,
        "chat": 5, "multimodal": 3, "long_context": 3, "speed": 4, "reliability": 4,
    },
    "deepseek": {
        "text_generation": 4, "structured_json": 4, "reasoning": 4,
        "analysis": 4, "coding": 5, "seo": 3, "marketing": 3,
        "chat": 4, "multimodal": 1, "long_context": 3, "speed": 4, "reliability": 4,
    },
    "gemini": {
        "text_generation": 4, "structured_json": 4, "reasoning": 3,
        "analysis": 4, "coding": 4, "seo": 4, "marketing": 4,
        "chat": 4, "multimodal": 5, "long_context": 5, "speed": 4, "reliability": 4,
    },
    "anthropic": {
        "text_generation": 5, "structured_json": 5, "reasoning": 5,
        "analysis": 5, "coding": 5, "seo": 4, "marketing": 4,
        "chat": 4, "multimodal": 3, "long_context": 5, "speed": 3, "reliability": 4,
    },
    "mock": {k: 1 for k in SKILLS},
}

DEFAULT_OPERATION_PROFILES: dict[str, dict[str, Any]] = {
    "chat.conversation": {
        "default_mode": "balanced", "complexity": 1, "minimum_quality": 3,
        "requirements": {"chat": 5, "text_generation": 4, "reliability": 4, "speed": 3, "long_context": 3},
        "reason": "Pracovní chat má být rychlý, ale zároveň konzistentní a spolehlivý při práci s projektovým kontextem.",
    },
    "analysis.project_profile": {
        "default_mode": "balanced", "complexity": 3, "minimum_quality": 4,
        "requirements": {"structured_json": 5, "analysis": 5, "reasoning": 4, "reliability": 5, "long_context": 4},
        "reason": "Profil projektu je dlouhodobá paměť aplikace; neplatná struktura je horší než mírně vyšší cena kvalitnějšího modelu.",
    },
    "analysis.brand_style": {
        "default_mode": "balanced", "complexity": 3, "minimum_quality": 4,
        "requirements": {"structured_json": 5, "analysis": 4, "reasoning": 4, "reliability": 5, "text_generation": 4},
        "reason": "Brand Style DNA musí být validní strukturovaný výstup z předzpracovaných vizuálních pozorování.",
    },
    "marketing.campaign.plan": {
        "default_mode": "balanced", "complexity": 3, "minimum_quality": 4,
        "requirements": {"structured_json": 5, "marketing": 5, "reasoning": 4, "reliability": 5, "long_context": 4},
        "reason": "Obsahový plán řídí navazující články, posty a publikaci, proto preferuje validitu a marketingovou kvalitu.",
    },
    "marketing.social.create": {
        "default_mode": "balanced", "complexity": 2, "minimum_quality": 3,
        "requirements": {"structured_json": 5, "marketing": 5, "text_generation": 4, "reliability": 4},
        "reason": "Social deriváty jsou opakovatelné, ale jejich JSON musí být validní a text marketingově použitelný.",
    },
    "marketing.article.create": {
        "default_mode": "balanced", "complexity": 3, "minimum_quality": 3,
        "requirements": {"structured_json": 5, "marketing": 4, "text_generation": 5, "reliability": 4, "long_context": 3},
        "reason": "Dlouhý marketingový článek ve strukturovaném JSON; priorita je kompletní a validní výstup před nejnižší cenou.",
    },
    "seo.metadata.optimize": {
        "default_mode": "economy", "complexity": 1, "minimum_quality": 2,
        "requirements": {"structured_json": 5, "seo": 3, "text_generation": 3, "reliability": 4, "speed": 3},
        "reason": "Hromadná, dobře strukturovaná SEO transformace; důležitá je validita výstupu a ekonomika.",
    },
    "seo.advisor": {
        "default_mode": "balanced", "complexity": 3, "minimum_quality": 3,
        "requirements": {"seo": 4, "analysis": 4, "reasoning": 3, "text_generation": 3, "reliability": 4},
        "reason": "SEO doporučení vyžaduje analýzu, ale ne automaticky nejdražší model.",
    },
}

MODE_WEIGHTS = {
    "economy": {"fit": 0.38, "quality": 0.08, "reliability": 0.14, "cost": 0.34, "speed": 0.06},
    "balanced": {"fit": 0.42, "quality": 0.18, "reliability": 0.20, "cost": 0.15, "speed": 0.05},
    "best": {"fit": 0.45, "quality": 0.25, "reliability": 0.24, "cost": 0.02, "speed": 0.04},
}
MODE_MIN_FIT = {"economy": 42.0, "balanced": 50.0, "best": 58.0}
_PERF_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_PERF_CACHE_TTL = 60.0


def invalidate_performance_cache(route_code: str | None = None, task_type: str | None = None, operation: str | None = None) -> None:
    """Invalidate cached empirical routing data after a fresh provider attempt."""
    if route_code is None:
        _PERF_CACHE.clear()
        return
    op = operation or ""
    task = task_type or ""
    keys = [key for key in _PERF_CACHE if key[0] == route_code and (not task or key[1] == task) and (not op or key[2] == op)]
    for key in keys:
        _PERF_CACHE.pop(key, None)


def _clamp_score(value: Any, default: int = 3) -> int:
    try:
        return max(1, min(5, int(round(float(value)))))
    except Exception:
        return default


def normalize_route_profile(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, raw in value.items():
        key = str(key).strip().lower()
        if key in SKILLS:
            out[key] = _clamp_score(raw)
    return out


def effective_model_profile(route) -> dict[str, int]:
    """Return local model knowledge, merged with optional admin override."""
    base = dict(DEFAULT_PROVIDER_PROFILES.get(route.provider, {}))
    quality = _clamp_score(getattr(route, "quality", 3))
    caps = {c.strip().lower() for c in (route.capabilities or "").split(",") if c.strip()}
    base.setdefault("text_generation", quality if "text" in caps else max(1, quality - 1))
    base.setdefault("structured_json", quality)
    base.setdefault("reasoning", quality)
    base.setdefault("speed", 3)
    base.setdefault("reliability", quality)
    for skill in ("analysis", "coding", "seo", "marketing", "chat"):
        if skill in caps:
            base[skill] = max(base.get(skill, 1), quality)
        else:
            base.setdefault(skill, max(1, quality - 1))
    raw = getattr(route, "routing_profile_json", None)
    if raw:
        try:
            override = normalize_route_profile(json.loads(raw))
            base.update(override)
        except Exception:
            pass
    return {skill: _clamp_score(base.get(skill, 1), 1) for skill in SKILLS}


def profile_json_for_admin(route) -> str:
    return json.dumps(effective_model_profile(route), ensure_ascii=False, sort_keys=True)


def _payload_size(context: dict | None) -> int:
    if not context:
        return 0
    try:
        return len(json.dumps(context.get("_engine_input_data") or {}, ensure_ascii=False))
    except Exception:
        return 0


def _batch_size(value: Any, depth: int = 0) -> int:
    if depth > 3:
        return 0
    if isinstance(value, list):
        return max(len(value), max((_batch_size(v, depth + 1) for v in value[:20]), default=0))
    if isinstance(value, dict):
        return max((_batch_size(v, depth + 1) for v in value.values()), default=0)
    return 0


def build_execution_profile(task_type: str, operation: str | None, prompt: str, context: dict | None,
                            requested_quality: str | None) -> dict[str, Any]:
    task = (task_type or "general").strip().lower()
    op = (operation or "").strip().lower() or None
    exact = dict(DEFAULT_OPERATION_PROFILES.get(op, {})) if op else {}
    requirements = dict(exact.get("requirements") or {})

    # Generic task knowledge when no operation-specific profile exists.
    generic = {
        "general": {"text_generation": 4, "reliability": 3},
        "chat": {"chat": 4, "text_generation": 3, "speed": 3, "reliability": 3},
        "marketing": {"marketing": 4, "text_generation": 4, "reliability": 3},
        "seo": {"seo": 4, "text_generation": 3, "reliability": 3},
        "analysis": {"analysis": 5, "reasoning": 4, "reliability": 4},
        "coding": {"coding": 5, "reasoning": 4, "structured_json": 2, "reliability": 4},
    }
    if not requirements:
        requirements.update(generic.get(task, generic["general"]))

    contract = (context or {}).get("_engine_output_contract") or {}
    if contract.get("type") == "json":
        requirements["structured_json"] = max(4, int(requirements.get("structured_json", 0) or 0))

    prompt_l = (prompt or "").lower()
    task_complexity = {"chat": 1, "general": 2, "marketing": 2, "seo": 2, "analysis": 3, "coding": 3}
    complexity = int(exact.get("complexity") or task_complexity.get(task, 2))
    if op:
        if any(marker in op for marker in (".metadata", ".rewrite", ".summarize", ".classify", ".extract", ".fix")):
            complexity = min(complexity, 2)
        if any(marker in op for marker in (".architecture", ".strategy", ".audit", ".security", ".deep_review", ".migration_plan")):
            complexity = max(complexity, 4)
    high_signals = (
        "strategie", "strategy", "architekt", "architecture", "hloubkov", "důklad", "audit",
        "bezpečnost", "security", "složit", "complex", "porovnej variant", "kritick",
    )
    if any(s in prompt_l for s in high_signals):
        complexity += 1
    if len(prompt or "") > 5000 or _payload_size(context) > 50000:
        complexity += 1
    batch = _batch_size((context or {}).get("_engine_input_data") or {})
    complexity = max(1, min(5, complexity))

    requested = (requested_quality or "auto").lower()
    if requested == "high":
        requested = "best"
    if requested in {"economy", "balanced", "best"}:
        mode = requested
        mode_source = "explicit_api"
    else:
        mode = str(exact.get("default_mode") or ("best" if complexity >= 4 else "balanced" if complexity >= 3 else "economy"))
        # Large repeatable batches should prefer economy unless task complexity itself is high.
        if batch >= 20 and complexity <= 2:
            mode = "economy"
        mode_source = "engine_auto"

    min_quality = int(exact.get("minimum_quality") or {"economy": 2, "balanced": 3, "best": 4}[mode])
    return {
        "task_type": task,
        "operation": op,
        "requested_quality": requested_quality or "auto",
        "mode": mode,
        "mode_source": mode_source,
        "complexity": complexity,
        "minimum_quality": max(1, min(5, min_quality)),
        "requirements": {k: max(1, min(5, int(v))) for k, v in requirements.items() if k in SKILLS},
        "batch_size": batch,
        "reason": exact.get("reason") or "Profil sestavený deterministicky z typu úlohy, operace, výstupního kontraktu a velikosti vstupu.",
    }


def capability_fit(route, task: str) -> tuple[float, str]:
    caps = {c.strip().lower() for c in (route.capabilities or "").split(",") if c.strip()}
    if task in caps:
        return 100.0, "exact_capability"
    # Text-capable models may still handle text-domain tasks. This is a soft
    # fallback, not a claim of equal specialization.
    if task in TEXT_TASKS and "text" in caps:
        if task == "coding":
            return 58.0, "generic_text_fallback"
        return 70.0, "generic_text_fallback"
    if "general" in caps and task in TEXT_TASKS:
        return 55.0, "general_fallback"
    return 0.0, "capability_mismatch"


def _empty_performance(history_error: str | None = None, feedback_error: str | None = None) -> dict[str, Any]:
    return {
        "samples": 0,
        "successes": 0,
        "invalid_outputs": 0,
        "technical_failures": 0,
        "success_rate": None,
        "avg_latency_ms": None,
        "avg_cost_usd": None,
        "feedback_samples": 0,
        "accepted": 0,
        "consecutive_invalid_outputs": 0,
        "last_attempt_at": None,
        "rejected_or_regenerated": 0,
        "acceptance_rate": None,
        "history_available": history_error is None,
        "feedback_available": feedback_error is None,
        "history_error": history_error,
        "feedback_error": feedback_error,
    }


def observed_performance(db, route_code: str, task_type: str, operation: str | None, limit: int = 200) -> dict[str, Any]:
    """Read Fresh AI's own execution history without ever blocking routing.

    Historical learning is an optimisation layer, not a runtime dependency.
    Missing/partially migrated history tables, transient DB errors or malformed
    legacy rows must never turn an otherwise valid AI request into HTTP 500.
    """
    cache_key = (route_code, task_type, operation or "")
    cached = _PERF_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _PERF_CACHE_TTL:
        return dict(cached[1])

    rows = []
    history_error = None
    try:
        stmt = (
            select(JobAttempt.status, JobAttempt.latency_ms, JobAttempt.estimated_cost_usd, JobAttempt.created_at)
            .join(Job, JobAttempt.job_id == Job.id)
            .where(JobAttempt.route_code == route_code)
        )
        if operation:
            stmt = stmt.where(Job.operation == operation)
        else:
            stmt = stmt.where(Job.task_type == task_type)
        rows = db.execute(stmt.order_by(JobAttempt.id.desc()).limit(limit)).all()
    except SQLAlchemyError as exc:
        # A failed SQL statement poisons the current transaction until rollback.
        # Recover locally and continue from static model knowledge.
        db.rollback()
        history_error = type(exc).__name__
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        history_error = type(exc).__name__

    total = len(rows)
    successes = sum(1 for status, _, _, _ in rows if status == "success")
    invalid = sum(1 for status, _, _, _ in rows if status == "invalid_output")
    failed = sum(1 for status, _, _, _ in rows if status == "failed")
    avg_latency = (sum(int(lat or 0) for _, lat, _, _ in rows) / total) if total else None
    avg_cost = (sum(float(cost or 0) for _, _, cost, _ in rows) / total) if total else None
    consecutive_invalid = 0
    for status, _, _, _ in rows:
        if status == "invalid_output":
            consecutive_invalid += 1
        else:
            break
    last_attempt_at = None
    if rows and rows[0][3] is not None:
        last_attempt_at = rows[0][3].isoformat()

    feedback = []
    feedback_error = None
    try:
        fstmt = (
            select(JobFeedback.outcome)
            .join(Job, JobFeedback.job_id == Job.id)
            .where(Job.selected_route_code == route_code)
        )
        if operation:
            fstmt = fstmt.where(Job.operation == operation)
        else:
            fstmt = fstmt.where(Job.task_type == task_type)
        feedback = [row[0] for row in db.execute(fstmt.order_by(JobFeedback.created_at.desc()).limit(limit)).all()]
    except SQLAlchemyError as exc:
        db.rollback()
        feedback_error = type(exc).__name__
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        feedback_error = type(exc).__name__

    accepted = sum(1 for x in feedback if x == "accepted")
    rejected = sum(1 for x in feedback if x in {"rejected", "regenerated"})
    result = _empty_performance(history_error, feedback_error)
    result.update({
        "samples": total,
        "successes": successes,
        "invalid_outputs": invalid,
        "technical_failures": failed,
        "success_rate": (successes / total) if total else None,
        "avg_latency_ms": avg_latency,
        "avg_cost_usd": avg_cost,
        "feedback_samples": len(feedback),
        "accepted": accepted,
        "consecutive_invalid_outputs": consecutive_invalid,
        "last_attempt_at": last_attempt_at,
        "rejected_or_regenerated": rejected,
        "acceptance_rate": (accepted / len(feedback)) if feedback else None,
        "history_available": history_error is None,
        "feedback_available": feedback_error is None,
    })
    _PERF_CACHE[cache_key] = (now, dict(result))
    return result


def fit_score(model_profile: dict[str, int], requirements: dict[str, int], capability_score: float) -> float:
    if not requirements:
        skill_fit = 60.0
    else:
        total_weight = sum(requirements.values())
        weighted = sum((model_profile.get(skill, 1) / 5.0 * 100.0) * weight for skill, weight in requirements.items())
        skill_fit = weighted / total_weight if total_weight else 60.0
    return round(skill_fit * 0.82 + capability_score * 0.18, 3)


def reliability_score(profile: dict[str, int], history: dict[str, Any]) -> float:
    prior = profile.get("reliability", 3) / 5.0 * 100.0
    score = prior
    samples = int(history.get("samples") or 0)
    if samples >= 5 and history.get("success_rate") is not None:
        # Repeated real failures must outweigh optimistic static model hints.
        # With dozens of samples, observed Fresh AI behaviour dominates almost
        # completely; this prevents a cheap route with 0% success from winning.
        evidence_weight = min(0.95, 0.35 + samples / 80.0)
        score = score * (1 - evidence_weight) + float(history["success_rate"]) * 100.0 * evidence_weight
    f_samples = int(history.get("feedback_samples") or 0)
    if f_samples >= 3 and history.get("acceptance_rate") is not None:
        evidence_weight = min(0.55, 0.20 + f_samples / 100.0)
        score = score * (1 - evidence_weight) + float(history["acceptance_rate"]) * 100.0 * evidence_weight
    return round(score, 3)


def speed_score(profile: dict[str, int], history: dict[str, Any], observed_latencies: list[float]) -> float:
    base = profile.get("speed", 3) / 5.0 * 100.0
    latency = history.get("avg_latency_ms")
    known = [x for x in observed_latencies if x and x > 0]
    if latency and known:
        fastest = min(known)
        empirical = min(100.0, fastest / float(latency) * 100.0)
        return round(base * 0.5 + empirical * 0.5, 3)
    return round(base, 3)


def relative_cost_score(cost: float | None, known_costs: list[float]) -> float:
    if cost is None:
        return 20.0
    positive = [c for c in known_costs if c is not None and c > 0]
    if not positive:
        return 60.0
    cheapest = min(positive)
    if cost <= 0:
        return 100.0
    ratio = max(1.0, cost / cheapest)
    # Smooth penalty: 1x=100, 2x~63, 5x~38, 10x~30.
    return round(100.0 / (1.0 + 0.85 * math.log(ratio)), 3)
