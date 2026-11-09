"""Fresh AI Engine Router 2.3: deterministic primary selection with provider failover guards.

The router is intentionally local. It does not call one model to decide which
other model to use and it does not probe providers until one works. It ranks
routes before execution from operation requirements, model knowledge, Fresh AI
Engine's observed performance, price and latency. Normal execution starts with rank #1. Additional providers are used only after an actual technical/provider failure or invalid required output; provider guards prevent repeated calls to an unhealthy platform.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import ModelRoute
from app.providers import build_providers
from app.capabilities import known_types, MEDIA_CAPABILITIES
from app.provider_guard import provider_guard_state
from app.routing_knowledge import (
    TEXT_TASKS,
    build_execution_profile,
    capability_fit,
    effective_model_profile,
    fit_score,
    observed_performance,
    reliability_score,
    relative_cost_score,
    speed_score,
    MODE_MIN_FIT,
    MODE_WEIGHTS,
)


@dataclass
class Candidate:
    route: ModelRoute
    score: float
    estimated_cost_usd: float | None
    details: dict = field(default_factory=dict)


KNOWN_TASK_TYPES = {"general", "chat", "analysis", "marketing", "seo", "coding"}


def _structured_circuit_state(history: dict, context: dict | None) -> dict:
    contract = (context or {}).get("_engine_output_contract") or {}
    if contract.get("type") != "json":
        return {"state": "closed", "reason": None, "cooldown_seconds": 1800}
    samples = int(history.get("samples") or 0)
    consecutive = int(history.get("consecutive_invalid_outputs") or 0)
    success_rate = history.get("success_rate")
    invalid = int(history.get("invalid_outputs") or 0)
    if samples < 3 or consecutive < 3:
        return {"state": "closed", "reason": None, "cooldown_seconds": 1800}
    invalid_rate = invalid / samples if samples else 0.0
    if success_rate is not None and float(success_rate) > 0.20:
        return {"state": "closed", "reason": None, "cooldown_seconds": 1800}
    if invalid_rate < 0.80:
        return {"state": "closed", "reason": None, "cooldown_seconds": 1800}

    last_raw = history.get("last_attempt_at")
    age = None
    if last_raw:
        try:
            last = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds())
        except Exception:
            age = None
    if age is None or age < 1800:
        return {
            "state": "open",
            "reason": "repeated_structured_output_invalid",
            "cooldown_seconds": 1800,
            "age_seconds": age,
            "invalid_rate": round(invalid_rate, 4),
            "consecutive_invalid_outputs": consecutive,
        }
    return {
        "state": "half_open",
        "reason": "cooldown_elapsed_probe_allowed",
        "cooldown_seconds": 1800,
        "age_seconds": age,
        "invalid_rate": round(invalid_rate, 4),
        "consecutive_invalid_outputs": consecutive,
    }


def estimate_tokens(text):
    return max(1, int(len(text) / 3.5))


def estimate_cost(route, prompt, max_output_tokens, context=None):
    if route.input_usd_per_million <= 0 and route.output_usd_per_million <= 0:
        return None  # unknown price is not a free model
    inp = estimate_tokens(prompt)
    if context:
        inp += estimate_tokens(json.dumps(context, ensure_ascii=False, separators=(",", ":")))
    expected_out = min(max_output_tokens, max(160, min(1000, int(inp * 0.8))))
    return (inp * route.input_usd_per_million + expected_out * route.output_usd_per_million) / 1_000_000


def ranked_candidates(db, task_type, quality, prompt, max_output_tokens, max_cost_usd,
                      force_route_code=None, context=None, operation=None):
    providers = build_providers()
    task = (task_type or "general").strip().lower()
    known = KNOWN_TASK_TYPES | known_types(db)
    execution = build_execution_profile(task, operation, prompt, context, quality)
    mode = execution["mode"]
    min_quality = int(execution["minimum_quality"])
    prelim, rows = [], []

    for route in db.scalars(select(ModelRoute).order_by(ModelRoute.priority.desc())).all():
        provider = providers.get(route.provider)
        cap_score, cap_reason = capability_fit(route, task)
        cost = estimate_cost(route, prompt, max_output_tokens, context=context)
        profile = effective_model_profile(route)
        history = observed_performance(db, route.code, task, operation)
        circuit = _structured_circuit_state(history, context)
        provider_guard = provider_guard_state(db, route.provider)
        reasons = []

        if not route.enabled:
            reasons.append("route_disabled")
        if provider is None:
            reasons.append("provider_unsupported")
        elif not provider.available():
            reasons.append("provider_not_configured")
        if provider_guard.get("state") == "open" and not force_route_code:
            reasons.append("provider_guard_open")
        if force_route_code and route.code != force_route_code:
            reasons.append("different_forced_route")
        caps = {c.strip().lower() for c in (route.capabilities or "").split(",") if c.strip()}
        if "text" not in caps:
            reasons.append("missing_text_capability")
        if task in MEDIA_CAPABILITIES:
            reasons.append("use_dedicated_media_api")
        if not force_route_code and cap_score <= 0 and task in known:
            reasons.append("task_capability_mismatch")
        if route.provider == "mock" and not force_route_code:
            reasons.append("mock_test_only")
        if circuit.get("state") == "open" and not force_route_code:
            reasons.append("structured_output_circuit_open")
        if route.quality < min_quality and not force_route_code:
            reasons.append("quality_below_required_profile")
        if max_cost_usd is not None:
            if cost is None and not force_route_code:
                reasons.append("price_unknown_for_budget")
            elif cost is not None and cost > max_cost_usd:
                reasons.append("budget_exceeded")

        fit = fit_score(profile, execution["requirements"], cap_score)
        if not force_route_code and fit < MODE_MIN_FIT[mode]:
            reasons.append("operation_fit_below_minimum")

        entry = {
            "code": route.code,
            "provider": route.provider,
            "model": route.model_name,
            "eligible": not reasons,
            "exclusions": reasons,
            "estimated_cost_usd": cost,
            "price_known": cost is not None,
            "quality": route.quality,
            "priority": route.priority,
            "capability_fit": round(cap_score, 3),
            "capability_source": cap_reason,
            "operation_fit": round(fit, 3),
            "model_profile": profile,
            "observed_performance": history,
            "circuit_breaker": circuit,
            "provider_guard": provider_guard,
            "score": None,
            "score_components": {},
        }
        rows.append(entry)
        if not reasons:
            prelim.append((route, cost, entry, profile, history, fit, circuit, provider_guard))

    known_costs = [cost for _, cost, *_ in prelim if cost is not None]
    observed_latencies = [float(item[4].get("avg_latency_ms") or 0) for item in prelim]
    weights = MODE_WEIGHTS[mode]
    options = []

    for route, cost, entry, profile, history, fit, circuit, provider_guard in prelim:
        rel = reliability_score(profile, history)
        speed = speed_score(profile, history, observed_latencies)
        cost_score = relative_cost_score(cost, known_costs)
        quality_score = route.quality / 5.0 * 100.0
        final = (
            fit * weights["fit"]
            + quality_score * weights["quality"]
            + rel * weights["reliability"]
            + cost_score * weights["cost"]
            + speed * weights["speed"]
        )
        circuit_penalty = 18.0 if circuit.get("state") == "half_open" else 0.0
        final -= circuit_penalty
        final = round(final, 4)
        entry["score"] = final
        entry["score_components"] = {
            "mode": mode,
            "fit": round(fit, 3),
            "quality": round(quality_score, 3),
            "reliability": round(rel, 3),
            "cost": round(cost_score, 3),
            "speed": round(speed, 3),
            "circuit_penalty": circuit_penalty,
            "weights": weights,
        }
        options.append((route, cost, entry, final))

    # Priority is deliberately only a final tie-breaker. The brain is operation
    # fit + observed performance + requested economics, not a fixed provider rank.
    options.sort(key=lambda item: (
        -item[3],
        item[1] is None,
        item[1] if item[1] is not None else float("inf"),
        -item[0].priority,
    ))

    candidates = []
    for i, (route, cost, entry, final) in enumerate(options):
        entry["selection_rank"] = i + 1
        candidates.append(Candidate(route=route, score=final, estimated_cost_usd=cost, details=entry))

    return candidates, rows


def route_candidates(db, task_type, quality, prompt, max_output_tokens, max_cost_usd,
                     force_route_code=None, context=None, operation=None):
    candidates, _ = ranked_candidates(
        db, task_type, quality, prompt, max_output_tokens, max_cost_usd,
        force_route_code, context, operation,
    )
    return candidates
