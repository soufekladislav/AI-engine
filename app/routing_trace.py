"""Transparent Router 2.0 trace using the same decision logic as execution."""
from app.router import ranked_candidates
from app.routing_knowledge import build_execution_profile


def inspect_routes(db, task_type, quality, prompt, max_output_tokens, max_cost_usd,
                   force_route_code=None, context=None, operation=None):
    execution = build_execution_profile(task_type, operation, prompt, context, quality)
    candidates, rows = ranked_candidates(
        db, task_type, quality, prompt, max_output_tokens, max_cost_usd,
        force_route_code, context=context, operation=operation,
    )
    if force_route_code:
        reason = "Vynucená route pro diagnostiku."
    elif candidates:
        top = candidates[0]
        reason = (
            f"Router 2.3 zvolil {top.route.code} před prvním provider voláním: "
            f"režim {execution['mode']}, operation-fit {top.details.get('operation_fit')}, "
            f"skóre {top.score:.2f}, odhad ${top.estimated_cost_usd:.8f}."
            if top.estimated_cost_usd is not None else
            f"Router 2.3 zvolil {top.route.code} před prvním provider voláním: "
            f"režim {execution['mode']}, operation-fit {top.details.get('operation_fit')}, skóre {top.score:.2f}."
        )
    else:
        reason = "Žádná dostupná route nesplnila požadavky operace, kvality a rozpočtu."
    return {
        "task_type": task_type,
        "operation": operation,
        "quality": quality,
        "routing_mode": execution["mode"],
        "execution_profile": execution,
        "max_cost_usd": max_cost_usd,
        "forced_route": force_route_code,
        "policy": "operation requirements -> model knowledge -> observed Fresh AI performance -> economics -> primary model; bounded recovery only after technical/structured failure",
        "ordered_candidates": [item.route.code for item in candidates],
        "selection_reason": reason,
        "routes": rows,
        "selected_route": candidates[0].route.code if candidates else None,
        "fallback_used": False,
        "provider_calls": 0,
    }
