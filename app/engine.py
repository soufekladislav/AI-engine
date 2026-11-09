import hashlib
import json
import time
from datetime import datetime, timezone
from sqlalchemy import select

from app.config import get_settings
from app.models import Job, JobAttempt, JobBilling, RuntimeSetting, RoutingDecision
from app.routing_trace import inspect_routes
from app.routing_knowledge import invalidate_performance_cache
from app.capabilities import assign_with_context
from app.providers import build_providers
from app.router import route_candidates
from app.schemas import EngineRequest
from app.local_memory import local_answer
from app.output_contracts import (
    contract_public_metadata,
    resolve_contract,
    validate_provider_output_detailed,
)
from app.multimodal import (
    available_image_generation_providers, generate_image_with_provider, understand_image,
    image_generation_request_parameters, image_request_cost_estimate,
)
from app.provider_guard import classify_provider_exception, mark_provider_failure, clear_provider_guard, provider_guard_state
from app.admin_alerts import add_admin_alert


def utcnow():
    return datetime.now(timezone.utc)


def _payload_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(raw: str | None, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
        return value
    except Exception:
        return default


def _declared_contract(req: EngineRequest) -> dict | None:
    if req.output_contract is None:
        return None
    return req.output_contract.model_dump(by_alias=True, exclude_none=True)


def create_job(db, req: EngineRequest) -> Job:
    s = get_settings()
    prompt_hash = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()
    preview = "[private chat request]" if req.source == "fresh-chat" else req.prompt[: s.log_preview_chars]
    media_op = _media_operation(req.operation)
    if media_op == "image.generate":
        # Operation has precedence over a legacy/general task_type. This is the
        # architectural guard that prevents Linda's task_type=general request
        # from ever entering the text router.
        resolved_task = "image_generation"
        assignment = {
            "source": "operation_override",
            "capability": "image_generation",
            "definition": "Dedicated image-generation media operation.",
            "matched_rule": "image.generate",
            "rule_priority": 1000,
        }
        contract = {"type": "image", "schema_id": "media.image.v1", "strict": True}
    elif media_op == "image.understand":
        resolved_task = "image_understanding"
        assignment = {
            "source": "operation_override",
            "capability": "image_understanding",
            "definition": "Dedicated image-understanding media operation.",
            "matched_rule": "image.understand",
            "rule_priority": 1000,
        }
        contract = {"type": "text", "schema_id": None, "strict": False}
    else:
        resolved_task, assignment = assign_with_context(db, req.prompt, req.task_type, req.context)
        contract = resolve_contract(req.operation, _declared_contract(req))
    contract_meta = contract_public_metadata(contract)

    job = Job(
        status="queued",
        source=req.source,
        project_id=req.project_id,
        user_id=req.user_id,
        task_type=resolved_task,
        operation=req.operation,
        quality=req.quality,
        max_cost_usd=req.max_cost_usd,
        max_output_tokens=req.max_output_tokens,
        prompt_hash=prompt_hash,
        prompt_preview=preview,
        prompt_payload=req.prompt if s.store_payloads else None,
        context_payload=_payload_text(req.context) if s.store_payloads else None,
        input_data_payload=_payload_text(req.input_data) if req.input_data else None,
        output_contract_payload=_payload_text(contract) if contract else None,
        result_type=(contract or {}).get("type") if contract else None,
        result_schema_id=(contract or {}).get("schema_id") if contract else None,
        result_validated=False,
    )
    # Worker needs the actual payload. If STORE_PAYLOADS=false, keep it transiently
    # until execution, then clear business data. Contract metadata remains on Job.
    if not s.store_payloads:
        job.prompt_payload = req.prompt
        job.context_payload = _payload_text(req.context)
        job.input_data_payload = _payload_text(req.input_data) if req.input_data else None
        job.output_contract_payload = _payload_text(contract) if contract else None
    db.add(job)
    db.flush()
    db.add(
        RoutingDecision(
            job_id=job.id,
            trace_json=json.dumps(
                {
                    "task_assignment": assignment,
                    "operation": req.operation,
                    "output_contract": contract_meta,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    db.refresh(job)
    return job


def _provider_context(job: Job) -> tuple[dict, dict | None]:
    context = _load_json(job.context_payload, {})
    input_data = _load_json(job.input_data_payload, {})
    contract = _load_json(job.output_contract_payload, None)
    if input_data:
        context["_engine_input_data"] = input_data
    if contract:
        context["_engine_output_contract"] = contract
    return context, contract


def _clear_transient_payloads(job: Job, store_payloads: bool) -> None:
    if store_payloads:
        return
    job.prompt_payload = None
    job.context_payload = None
    job.input_data_payload = None
    job.output_contract_payload = None


def _record_contract_trace(trace: dict, contract: dict | None, **extra) -> None:
    block = contract_public_metadata(contract) or {"type": "text", "schema_id": None, "strict": False}
    block.update(extra)
    trace["output_contract"] = block


def fail_job_safely(db, job: Job, category: str, exc: Exception | None = None, trace_extra: dict | None = None) -> Job:
    """Convert an unexpected internal execution error into a normal failed job.

    This keeps connector/API calls deterministic: application bugs or optional
    routing diagnostics must not escape as an unstructured HTTP 500 after a job
    has already been created/reserved.
    """
    try:
        db.rollback()
    except Exception:
        pass
    message = category
    if exc is not None:
        message += f": {type(exc).__name__}"
    job.status = "failed"
    job.error = message[:500]
    job.result_validated = False
    job.finished_at = utcnow()
    try:
        decision = db.get(RoutingDecision, job.id)
        if decision is None:
            decision = RoutingDecision(job_id=job.id, trace_json="{}")
            db.add(decision)
        trace = _load_json(decision.trace_json, {})
        trace["internal_error"] = {"category": category, "type": type(exc).__name__ if exc else None}
        trace["fallback_used"] = False
        trace.setdefault("provider_calls", 0)
        if trace_extra:
            trace.update(trace_extra)
        decision.trace_json = json.dumps(trace, ensure_ascii=False)
        _clear_transient_payloads(job, get_settings().store_payloads)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return job
    try:
        from app.accounts import settle_job
        settle_job(db, job)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return job


def _media_operation(operation: str | None) -> str | None:
    op = (operation or "").strip().lower()
    aliases = {
        "image.generate": "image.generate",
        "image_generation.generate": "image.generate",
        "image.understand": "image.understand",
        "image_understand": "image.understand",
    }
    return aliases.get(op)


def _execute_media_job(db, job: Job, prompt: str, context: dict, contract: dict | None) -> Job:
    """Execute media operations without entering the generic text router.

    ``image.generate`` uses an image-only adapter registry. A future provider is
    added there; it never becomes eligible merely because it can answer text.
    """
    s = get_settings()
    op = _media_operation(job.operation)
    input_data = _load_json(job.input_data_payload, {})
    decision = db.get(RoutingDecision, job.id)
    if decision is None:
        decision = RoutingDecision(job_id=job.id, trace_json="{}")
        db.add(decision)
    saved = _load_json(decision.trace_json, {})
    trace = {
        "task_type": job.task_type,
        "capability": "image_generation" if op == "image.generate" else "image_understanding",
        "operation": job.operation,
        "task_assignment": saved.get("task_assignment", {"source": "media_operation", "capability": job.task_type}),
        "policy": "dedicated media adapter; image operations never enter text-model routing",
        "selection_reason": "Operation-first media dispatch selected a capability-specific adapter before generic routing.",
        "routes": [],
        "selected_route": None,
        "fallback_used": False,
        "provider_calls": 0,
        "media_operation": op,
    }
    _record_contract_trace(trace, contract, validated=False, repaired=False, provider_calls=0)

    if op == "image.generate":
        params = image_generation_request_parameters(input_data, job.quality)
        trace["execution_profile"] = {
            "task_type": "image_generation",
            "operation": "image.generate",
            "requested_quality": job.quality,
            "mode": "media",
            "mode_source": "operation_override",
            "complexity": 3,
            "minimum_quality": 1,
            "requirements": {"image_generation": 5, "image_output": 5, "reliability": 4},
            "reason": "Obrazová operace vyžaduje skutečný image output; textové routy nejsou způsobilé.",
        }
        specs = available_image_generation_providers()
        candidates = []
        for spec in specs:
            guard = provider_guard_state(db, spec.provider)
            eligible = bool(spec.configured and guard.get("state") != "open")
            candidates.append({
                "provider": spec.provider,
                "adapter": spec.adapter,
                "model": params["model"] if spec.provider == "openai" else spec.default_model,
                "configured": spec.configured,
                "guard": guard,
                "eligible": eligible,
            })
        trace["image_candidates"] = candidates
        trace["ordered_candidates"] = [c["provider"] for c in candidates if c["eligible"]]
        trace["media_contract"] = {"type": "image", "kind": "image", "requires_binary_asset": True}

        estimated = image_request_cost_estimate(params["model"], params["quality"], params["size"])
        trace["preflight_cost_usd"] = estimated
        if job.max_cost_usd is not None and estimated > float(job.max_cost_usd):
            job.status = "failed"
            job.error = "image_generation_budget_exceeded"
            job.result_type = "image"
            job.result_validated = False
            job.estimated_cost_usd = 0.0
            job.latency_ms = 0
            job.finished_at = utcnow()
            trace["error_category"] = "image_generation_budget_exceeded"
            decision.trace_json = json.dumps(trace, ensure_ascii=False)
            _clear_transient_payloads(job, s.store_payloads)
            db.commit()
            try:
                from app.accounts import settle_job
                settle_job(db, job)
            except Exception:
                pass
            return job

        eligible_specs = [spec for spec, c in zip(specs, candidates) if c["eligible"]]
        if not eligible_specs:
            job.status = "failed"
            job.error = "image_generation_unavailable"
            job.result_type = "image"
            job.result_validated = False
            job.finished_at = utcnow()
            trace["error_category"] = "image_generation_unavailable"
            trace["provider_calls"] = 0
            decision.trace_json = json.dumps(trace, ensure_ascii=False)
            _clear_transient_payloads(job, s.store_payloads)
            db.commit()
            try:
                from app.accounts import settle_job
                settle_job(db, job)
            except Exception:
                pass
            return job

        overall_started = time.perf_counter()
        attempts_trace = []
        total_cost = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        first_provider = eligible_specs[0].provider
        trace["initial_selected_provider"] = first_provider
        trace["initial_selected_route"] = f"{first_provider}-image-generation"
        trace["selected_route"] = trace["initial_selected_route"]

        for spec in eligible_specs:
            live_guard = provider_guard_state(db, spec.provider)
            if live_guard.get("state") == "open":
                attempts_trace.append({"provider": spec.provider, "status": "provider_guard_skipped", "guard": live_guard})
                continue
            model = params["model"] if spec.provider == "openai" else spec.default_model
            route_code = f"{spec.provider}-image-generation"
            job.selected_route_code = route_code
            job.provider = spec.provider
            job.model_name = model
            attempt = JobAttempt(job_id=job.id, route_code=route_code, provider=spec.provider, model_name=model)
            db.add(attempt)
            db.commit()
            started = time.perf_counter()
            trace["provider_calls"] = int(trace.get("provider_calls") or 0) + 1
            try:
                result = generate_image_with_provider(
                    spec.provider,
                    prompt,
                    model=model,
                    size=params["size"],
                    quality=params["quality"],
                    output_format=params["output_format"],
                    background=params["background"],
                    output_compression=params.get("output_compression"),
                )
                latency = int((time.perf_counter() - started) * 1000)
                if result.get("kind") != "image" or (not result.get("base64") and not result.get("url")):
                    raise RuntimeError("Image provider returned no usable image asset")
                usage = result.get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                actual_cost = float(result.get("provider_cost_usd") or 0.0)
                total_cost = round(total_cost + actual_cost, 8)
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                attempt.status = "success"
                attempt.latency_ms = latency
                attempt.input_tokens = input_tokens
                attempt.output_tokens = output_tokens
                attempt.estimated_cost_usd = actual_cost
                clear_provider_guard(db, spec.provider)

                job.status = "completed"
                job.error = None
                job.provider = str(result.get("provider") or spec.provider)
                job.model_name = str(result.get("model") or model)
                job.response_preview = "[generated image asset]"
                # Binary media is intentionally never persisted in Job payloads.
                # The synchronous caller receives it through _fresh_result.
                job.response_payload = None
                job.result_type = "image"
                job.result_schema_id = "media.image.v1"
                job.result_validated = True
                job.input_tokens = total_input_tokens
                job.output_tokens = total_output_tokens
                job.estimated_cost_usd = total_cost
                job.latency_ms = int((time.perf_counter() - overall_started) * 1000)
                job.finished_at = utcnow()

                asset_meta = {
                    "kind": "image",
                    "mime_type": result.get("mime_type"),
                    "width": result.get("width"),
                    "height": result.get("height"),
                    "size_bytes": result.get("size_bytes"),
                    "quality": result.get("quality"),
                    "requested_size": params.get("size"),
                    "provider_size": result.get("size"),
                    "output_format": result.get("output_format"),
                    "background": result.get("background"),
                    "cost_usd": actual_cost,
                    "cost_source": result.get("cost_source"),
                }
                attempts_trace.append({
                    "route": route_code,
                    "provider": job.provider,
                    "model": job.model_name,
                    "status": "success",
                    "latency_ms": latency,
                    "cost_usd": actual_cost,
                    **{k: asset_meta[k] for k in ("mime_type", "width", "height", "size_bytes")},
                })
                trace.update({
                    "selected_route": route_code,
                    "final_selected_route": route_code,
                    "final_selected_provider": job.provider,
                    "selected_model": job.model_name,
                    "fallback_used": job.provider != first_provider,
                    "single_shot": int(trace["provider_calls"]) == 1,
                    "attempts": attempts_trace,
                    "media_asset": asset_meta,
                    "usage": usage,
                    "cost_usd": total_cost,
                })
                if trace["fallback_used"]:
                    trace["recovery_trigger"] = "image_provider_failure"
                    _add_failover_success_alert(db, job, first_provider, job.provider, "image_provider_failure", attempts_trace)
                _record_contract_trace(trace, contract, validated=True, repaired=False, provider_calls=int(trace["provider_calls"]))
                decision.trace_json = json.dumps(trace, ensure_ascii=False)
                _clear_transient_payloads(job, s.store_payloads)
                db.commit()

                try:
                    from app.accounts import charge_job
                    charge_job(db, job)
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                bill = db.get(JobBilling, job.id)
                billing_meta = {
                    "billing_applied": bool(bill),
                    "reserved_credits": float(bill.reserved_credits or 0) if bill else 0.0,
                    "credits_charged": float(bill.credits_charged or 0) if bill else 0.0,
                    "cost_usd": float(bill.cost_usd or total_cost) if bill else total_cost,
                }
                trace["credits"] = billing_meta
                decision.trace_json = json.dumps(trace, ensure_ascii=False)
                db.commit()

                # Public result contains the asset but not internal billing IDs.
                public_result = {
                    "kind": "image",
                    "mime_type": result.get("mime_type"),
                    "url": result.get("url"),
                    "base64": result.get("base64"),
                    "width": result.get("width"),
                    "height": result.get("height"),
                    "size_bytes": result.get("size_bytes"),
                }
                job._fresh_output = None
                job._fresh_result = public_result
                job._fresh_billing = billing_meta
                return job
            except Exception as exc:
                latency = int((time.perf_counter() - started) * 1000)
                failure = classify_provider_exception(spec.provider, exc)
                guard = mark_provider_failure(db, failure)
                attempt.status = "failed"
                attempt.latency_ms = latency
                attempt.error = f"{failure.category}: {failure.technical_detail[:500]}"
                attempts_trace.append({
                    "route": route_code,
                    "provider": spec.provider,
                    "model": model,
                    "status": "provider_failed",
                    "latency_ms": latency,
                    "error_category": failure.category,
                    "guard": guard,
                })
                _add_provider_failure_alert(db, job, failure, guard)
                trace["recovery_trigger"] = failure.category
                trace["attempts"] = attempts_trace
                decision.trace_json = json.dumps(trace, ensure_ascii=False)
                db.commit()
                continue

        # Every real image provider failed or was skipped. Do not substitute text.
        job.status = "failed"
        job.error = "image_generation_failed" if int(trace.get("provider_calls") or 0) else "image_generation_unavailable"
        job.result_type = "image"
        job.result_schema_id = "media.image.v1"
        job.result_validated = False
        job.response_payload = None
        job.estimated_cost_usd = total_cost
        job.input_tokens = total_input_tokens
        job.output_tokens = total_output_tokens
        job.latency_ms = int((time.perf_counter() - overall_started) * 1000)
        job.finished_at = utcnow()
        trace["error_category"] = job.error
        trace["attempts"] = attempts_trace
        trace["final_selected_provider"] = job.provider
        trace["fallback_used"] = int(trace.get("provider_calls") or 0) > 1
        _record_contract_trace(trace, contract, validated=False, repaired=False, provider_calls=int(trace.get("provider_calls") or 0))
        decision.trace_json = json.dumps(trace, ensure_ascii=False)
        _clear_transient_payloads(job, s.store_payloads)
        db.commit()
        try:
            from app.accounts import settle_job
            settle_job(db, job)
        except Exception:
            pass
        return job

    # Existing image-understanding path remains synchronous and isolated from
    # text routing. It is intentionally not treated as image generation.
    started = time.perf_counter()
    attempt = None
    try:
        if op == "image.understand":
            model = input_data.get("model") or None
            mime_type = str(input_data.get("mime_type") or "image/jpeg")
            image_base64 = input_data.get("image_base64")
            route_code = "openai-image-understanding"
            attempt = JobAttempt(job_id=job.id, route_code=route_code, provider="openai", model_name=str(model or "vision-default"))
            db.add(attempt)
            db.commit()
            result = understand_image(prompt, image_base64, mime_type=mime_type, model=model)
            output = str(result.get("output") or "").strip()
            if not output:
                raise RuntimeError("Vision provider returned empty output")
            usage = result.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            latency = int((time.perf_counter() - started) * 1000)
            attempt.status = "success"
            attempt.latency_ms = latency
            attempt.input_tokens = input_tokens
            attempt.output_tokens = output_tokens
            attempt.estimated_cost_usd = 0.0
            job.status = "completed"
            job.selected_route_code = route_code
            job.provider = "openai"
            job.model_name = str(result.get("model") or model or "vision-default")
            job.response_preview = "[private image understanding response]" if job.source == "fresh-chat" else output[: s.log_preview_chars]
            job.response_payload = output if s.store_payloads else None
            job.result_type = "text"
            job.result_schema_id = None
            job.result_validated = True
            job.input_tokens = input_tokens
            job.output_tokens = output_tokens
            job.estimated_cost_usd = 0.0
            job.latency_ms = latency
            job.finished_at = utcnow()
            trace.update({"selected_route": route_code, "selected_model": job.model_name, "result_type": "text", "provider_calls": 1})
            _record_contract_trace(trace, contract, validated=True, repaired=False, provider_calls=1)
            decision.trace_json = json.dumps(trace, ensure_ascii=False)
            _clear_transient_payloads(job, s.store_payloads)
            db.commit()
            try:
                from app.accounts import charge_job
                charge_job(db, job)
            except Exception:
                pass
            job._fresh_output = output
            job._fresh_result = output
            return job
        raise ValueError("Unsupported media operation")
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        provider_name = attempt.provider if attempt is not None else "openai"
        failure = classify_provider_exception(provider_name, exc)
        guard = mark_provider_failure(db, failure)
        if attempt is not None:
            attempt.status = "failed"
            attempt.latency_ms = latency
            attempt.error = f"{failure.category}: {failure.technical_detail[:500]}"
        trace["provider_error"] = {
            "provider": provider_name,
            "category": failure.category,
            "status_code": failure.status_code,
            "guard": guard,
        }
        _add_provider_failure_alert(db, job, failure, guard)
        decision.trace_json = json.dumps(trace, ensure_ascii=False)
        job.status = "failed"
        job.error = "media_understanding_failed"
        job.result_type = "text"
        job.result_validated = False
        job.latency_ms = latency
        job.finished_at = utcnow()
        _clear_transient_payloads(job, s.store_payloads)
        db.commit()
        try:
            from app.accounts import settle_job
            settle_job(db, job)
        except Exception:
            pass
        return job

def _recovery_order(candidates, requested_quality: str):
    """Order backup routes for resilience, not for speculative quality probing.

    Primary routing still decides the best model before execution. Once that
    provider has *actually failed*, backup selection prioritizes known success
    and sensible cost. This prevents a recovery step from jumping straight to
    a dramatically more expensive provider when a cheaper eligible platform is
    available.
    """
    backups = list(candidates)

    def key(c):
        d = c.details or {}
        h = d.get("observed_performance") or {}
        samples = int(h.get("samples") or 0)
        success_rate = h.get("success_rate")
        invalid = int(h.get("invalid_outputs") or 0)
        technical = int(h.get("technical_failures") or 0)
        known_success = 0 if (success_rate is not None and float(success_rate) > 0) else 1
        known_bad = 1 if samples >= 2 and (success_rate == 0 or invalid >= 2 or technical >= 2) else 0
        cost = float(c.estimated_cost_usd) if c.estimated_cost_usd is not None else float("inf")
        if requested_quality in {"high", "best"}:
            return (known_bad, known_success, -float(c.score), cost)
        return (known_bad, known_success, cost, -float(c.score))

    backups.sort(key=key)
    return backups


def _add_provider_failure_alert(db, job: Job, failure, guard: dict) -> None:
    until = guard.get("blocked_until") or ""
    title = f"Provider {failure.provider} dočasně vyřazen"
    message = (
        f"Úloha {job.operation or job.task_type} ({job.id}) narazila na {failure.category}. "
        f"Engine bude používat jinou platformu. Provider guard do: {until}."
    )
    add_admin_alert(
        db,
        alert_type="provider_guard",
        severity="warning",
        title=title,
        message=message,
        dedupe_key=f"provider_guard:{failure.provider}:{failure.category}",
        metadata={
            "job_id": job.id,
            "provider": failure.provider,
            "category": failure.category,
            "status_code": failure.status_code,
            "blocked_until": until,
        },
    )


def _add_failover_success_alert(db, job: Job, primary_provider: str, final_provider: str,
                                trigger: str | None, attempts_trace: list[dict]) -> None:
    add_admin_alert(
        db,
        alert_type="provider_failover",
        severity="info",
        title="Úloha dokončena přes záložní platformu",
        message=(
            f"{job.operation or job.task_type} ({job.id}) začala na {primary_provider}, "
            f"ale po selhání byla úspěšně dokončena přes {final_provider}. "
            f"Důvod přepnutí: {trigger or 'provider_failure'}."
        ),
        dedupe_key=f"failover:{job.id}",
        metadata={
            "job_id": job.id,
            "operation": job.operation,
            "primary_provider": primary_provider,
            "final_provider": final_provider,
            "trigger": trigger,
            "attempts": attempts_trace[-4:],
        },
    )


def execute_job(db, job: Job, force_route_code: str | None = None) -> Job:
    """Execute one routed task with bounded cross-provider failover.

    Router still chooses one primary model before execution. We never call
    several providers merely to compare answer quality. A second/third/fourth
    provider is used only after an actual provider/transport failure, or after
    an invalid required output. Technical failures (billing, auth, 429, 5xx,
    timeout/network) may walk across up to four providers because those calls
    generally do not produce a usable paid result. Invalid content is capped at
    two content attempts to prevent cost runaway.
    """
    s = get_settings()
    prompt = job.prompt_payload or ""
    context, contract = _provider_context(job)
    job.status = "running"
    job.started_at = utcnow()
    job.error = None
    db.commit()

    if _media_operation(job.operation):
        return _execute_media_job(db, job, prompt, context, contract)

    local = (
        local_answer(prompt, context)
        if job.source == "fresh-chat"
        and context.get("_chat_memory_verified") is True
        and force_route_code is None
        and (not contract or contract.get("type") == "text")
        else None
    )
    if local:
        output, evidence = local
        decision = db.get(RoutingDecision, job.id)
        if decision is None:
            decision = RoutingDecision(job_id=job.id)
            db.add(decision)
        local_trace = {
            "selected_route": "local-memory",
            "task_type": job.task_type,
            "operation": job.operation,
            "selection_reason": evidence["reason"],
            "memory_ids": evidence.get("memory_ids", []),
            "provider_calls": 0,
            "fallback_used": False,
            "policy": "local deterministic answer before external routing",
        }
        _record_contract_trace(local_trace, contract, validated=True, repaired=False)
        decision.trace_json = json.dumps(local_trace, ensure_ascii=False)
        job.status = "completed"
        job.selected_route_code = None
        job.provider = "local"
        job.model_name = "memory-v1"
        job.response_preview = "[private local memory response]"
        job.response_payload = output if s.store_payloads else None
        job.result_type = "text"
        job.result_schema_id = None
        job.result_validated = True
        job.input_tokens = 0
        job.output_tokens = 0
        job.estimated_cost_usd = 0.0
        job.latency_ms = 0
        job.finished_at = utcnow()
        _clear_transient_payloads(job, s.store_payloads)
        db.commit()
        try:
            from app.accounts import charge_job
            charge_job(db, job)
        except Exception:
            pass
        job._fresh_output = output
        job._fresh_result = output
        return job

    try:
        trace = inspect_routes(
            db, job.task_type, job.quality, prompt, job.max_output_tokens,
            job.max_cost_usd, force_route_code, context=context, operation=job.operation,
        )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        trace = {
            "task_type": job.task_type,
            "operation": job.operation,
            "quality": job.quality,
            "policy": "Router 2.3 primary selection; diagnostics unavailable, execution continued",
            "selection_reason": "Routing diagnostics were unavailable; static routing knowledge remained active.",
            "routes": [],
            "ordered_candidates": [],
            "selected_route": None,
            "fallback_used": False,
            "provider_calls": 0,
            "diagnostic_error": type(exc).__name__,
        }
    decision = db.get(RoutingDecision, job.id)
    if decision is None:
        decision = RoutingDecision(job_id=job.id)
        db.add(decision)
    saved_trace = _load_json(decision.trace_json, {})
    trace["task_assignment"] = saved_trace.get("task_assignment", {"source": "legacy_job", "capability": job.task_type})
    trace["operation"] = saved_trace.get("operation") or job.operation
    _record_contract_trace(trace, contract, validated=False, repaired=False, provider_calls=0)
    decision.trace_json = json.dumps(trace, ensure_ascii=False)
    db.commit()

    try:
        candidates = route_candidates(
            db, job.task_type, job.quality, prompt, job.max_output_tokens,
            job.max_cost_usd, force_route_code=force_route_code,
            context=context, operation=job.operation,
        )
    except Exception as exc:
        return fail_job_safely(
            db, job, "routing_internal_error", exc,
            {"routing_phase": "candidate_selection", "provider_calls": 0},
        )
    if not candidates:
        job.status = "failed"
        job.error = "No available model route matches this operation/profile/budget."
        job.finished_at = utcnow()
        _clear_transient_payloads(job, s.store_payloads)
        db.commit()
        try:
            from app.accounts import settle_job
            settle_job(db, job)
        except Exception:
            pass
        return job

    structured = bool(contract and contract.get("type") == "json")
    primary = candidates[0]
    ordered = [primary]
    if force_route_code is None:
        ordered.extend(_recovery_order(candidates[1:], job.quality))

    max_provider_calls = 1 if force_route_code else min(4, len(ordered))
    max_invalid_content_attempts = 2
    providers = build_providers()
    overall_started = time.perf_counter()
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    attempts_trace: list[dict] = []
    last_errors: list[str] = []
    last_raw_text = ""
    actual_provider_calls = 0
    invalid_content_attempts = 0
    recovery_trigger: str | None = None
    primary_provider = primary.route.provider

    trace["initial_selected_route"] = primary.route.code
    trace["initial_selected_provider"] = primary_provider
    trace["selected_route"] = primary.route.code
    trace["fallback_used"] = False
    trace["provider_calls"] = 0
    trace["single_shot"] = max_provider_calls == 1
    trace["recovery_policy"] = (
        "cross-provider failover after actual provider/transport failure; at most two invalid-content attempts"
        if max_provider_calls > 1 else "single forced provider call"
    )
    trace["max_provider_calls"] = max_provider_calls
    trace["max_invalid_content_attempts"] = max_invalid_content_attempts
    trace["selected_score"] = primary.score
    decision.trace_json = json.dumps(trace, ensure_ascii=False)
    db.commit()

    for selected in ordered:
        if actual_provider_calls >= max_provider_calls:
            break
        r = selected.route

        # Re-check the provider guard because a previous attempt in this same
        # request may just have opened it, or another Passenger process may have.
        live_guard = provider_guard_state(db, r.provider)
        if live_guard.get("state") == "open" and force_route_code is None:
            attempts_trace.append({
                "route": r.code,
                "provider": r.provider,
                "model": r.model_name,
                "status": "provider_guard_skipped",
                "guard": live_guard,
            })
            continue

        if actual_provider_calls > 0 and job.max_cost_usd is not None:
            projected = total_cost + float(selected.estimated_cost_usd or 0.0)
            if projected > float(job.max_cost_usd):
                last_errors.append("recovery_budget_exceeded")
                attempts_trace.append({
                    "route": r.code, "provider": r.provider, "model": r.model_name,
                    "status": "budget_skipped", "projected_cost_usd": round(projected, 8),
                })
                continue

        provider = providers.get(r.provider)
        if provider is None:
            last_errors.append(f"provider_unsupported:{r.provider}")
            attempts_trace.append({
                "route": r.code, "provider": r.provider, "model": r.model_name,
                "status": "provider_unsupported",
            })
            continue

        job.selected_route_code = r.code
        job.provider = r.provider
        job.model_name = r.model_name
        db.commit()

        attempt = JobAttempt(job_id=job.id, route_code=r.code, provider=r.provider, model_name=r.model_name)
        db.add(attempt)
        db.commit()
        started = time.perf_counter()
        actual_provider_calls += 1
        trace["provider_calls"] = actual_provider_calls
        if actual_provider_calls > 1:
            trace["fallback_used"] = True
            trace["recovery_route"] = r.code

        try:
            result = provider.generate(r.model_name, prompt, context, job.max_output_tokens)
            latency = int((time.perf_counter() - started) * 1000)
            actual_cost = _actual_cost(r, result.input_tokens, result.output_tokens)
            total_input_tokens += int(result.input_tokens or 0)
            total_output_tokens += int(result.output_tokens or 0)
            total_cost = round(total_cost + actual_cost, 8)
            attempt.latency_ms = latency
            attempt.input_tokens = result.input_tokens
            attempt.output_tokens = result.output_tokens
            attempt.estimated_cost_usd = actual_cost
            last_raw_text = result.text or ""

            valid, normalized_result, canonical_output, errors, validation_meta = validate_provider_output_detailed(
                result.text, contract
            )
            if valid:
                attempt.status = "success"
                attempts_trace.append({
                    "route": r.code,
                    "provider": r.provider,
                    "model": r.model_name,
                    "status": "success",
                    "latency_ms": latency,
                    "repaired": bool(validation_meta.get("repaired")),
                    "cost_usd": actual_cost,
                })
                clear_provider_guard(db, r.provider)
                job.status = "completed"
                job.error = None
                job.selected_route_code = r.code
                job.provider = r.provider
                job.model_name = r.model_name
                job.response_preview = (
                    "[private chat response]" if job.source == "fresh-chat" else canonical_output[: s.log_preview_chars]
                )
                job.response_payload = canonical_output if s.store_payloads else None
                job.result_type = (contract or {}).get("type") or "text"
                job.result_schema_id = (contract or {}).get("schema_id")
                job.result_validated = True
                job.input_tokens = total_input_tokens
                job.output_tokens = total_output_tokens
                job.estimated_cost_usd = total_cost
                job.latency_ms = int((time.perf_counter() - overall_started) * 1000)
                job.finished_at = utcnow()
                trace["selected_route"] = r.code
                trace["final_selected_route"] = r.code
                trace["final_selected_provider"] = r.provider
                trace["attempts"] = attempts_trace
                trace["provider_calls"] = actual_provider_calls
                trace["fallback_used"] = actual_provider_calls > 1
                trace["single_shot"] = actual_provider_calls == 1
                if recovery_trigger:
                    trace["recovery_trigger"] = recovery_trigger
                _record_contract_trace(
                    trace, contract, validated=True,
                    repaired=bool(validation_meta.get("repaired")),
                    repair_notes=validation_meta.get("repair_notes") or [],
                    validation_errors=[], provider_calls=actual_provider_calls,
                )
                if actual_provider_calls > 1:
                    _add_failover_success_alert(
                        db, job, primary_provider, r.provider, recovery_trigger, attempts_trace
                    )
                decision.trace_json = json.dumps(trace, ensure_ascii=False)
                _clear_transient_payloads(job, s.store_payloads)
                db.commit()
                invalidate_performance_cache(r.code, job.task_type, job.operation)
                try:
                    from app.accounts import charge_job
                    charge_job(db, job)
                except Exception:
                    pass
                job._fresh_output = canonical_output
                job._fresh_result = normalized_result
                return job

            # Provider returned a response, but it did not satisfy the contract.
            invalid_content_attempts += 1
            attempt.status = "invalid_output"
            attempt.error = (("structured_output_invalid" if structured else "invalid_output") + ": " + "; ".join(errors[:6]))[:1000]
            last_errors = errors[:20]
            attempts_trace.append({
                "route": r.code,
                "provider": r.provider,
                "model": r.model_name,
                "status": "invalid_output",
                "latency_ms": latency,
                "cost_usd": actual_cost,
                "validation_errors": errors[:6],
                "local_repair_attempted": bool(validation_meta.get("repair_notes")),
                "local_repair_succeeded": False,
            })
            db.commit()
            invalidate_performance_cache(r.code, job.task_type, job.operation)
            recovery_trigger = "structured_output_invalid" if structured else "empty_or_invalid_output"
            if force_route_code or invalid_content_attempts >= max_invalid_content_attempts:
                break
            continue

        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            failure = classify_provider_exception(r.provider, exc)
            guard = mark_provider_failure(db, failure)
            err = f"{failure.category}: {failure.technical_detail[:500]}"
            attempt.status = "failed"
            attempt.latency_ms = latency
            attempt.error = err[:1000]
            last_errors = [failure.public_message]
            attempts_trace.append({
                "route": r.code,
                "provider": r.provider,
                "model": r.model_name,
                "status": "provider_failed",
                "latency_ms": latency,
                "failure_category": failure.category,
                "status_code": failure.status_code,
                "provider_guard": guard,
            })
            recovery_trigger = f"provider_failure:{failure.category}"
            trace["recovery_trigger"] = recovery_trigger
            _add_provider_failure_alert(db, job, failure, guard)
            db.commit()
            invalidate_performance_cache(r.code, job.task_type, job.operation)
            if force_route_code:
                break
            # Technical/provider failure does not consume the invalid-content
            # allowance. Continue to another independent platform.
            continue

    job.status = "failed"
    category = "structured_output_invalid" if structured and any(
        a.get("status") == "invalid_output" for a in attempts_trace
    ) else "provider_execution_failed"
    job.error = (category + (": " + "; ".join(last_errors[:6]) if last_errors else ""))[:1000]
    job.response_preview = "[private invalid response]" if job.source == "fresh-chat" else last_raw_text[: s.log_preview_chars]
    job.response_payload = last_raw_text if (s.store_payloads and last_raw_text) else None
    job.result_type = (contract or {}).get("type") if contract else None
    job.result_schema_id = (contract or {}).get("schema_id") if contract else None
    job.result_validated = False
    job.input_tokens = total_input_tokens
    job.output_tokens = total_output_tokens
    job.estimated_cost_usd = total_cost
    job.latency_ms = int((time.perf_counter() - overall_started) * 1000)
    job.finished_at = utcnow()
    trace["attempts"] = attempts_trace
    trace["provider_calls"] = actual_provider_calls
    trace["fallback_used"] = actual_provider_calls > 1
    trace["single_shot"] = actual_provider_calls <= 1
    trace["final_selected_route"] = job.selected_route_code
    trace["final_selected_provider"] = job.provider
    if recovery_trigger:
        trace["recovery_trigger"] = recovery_trigger
    if total_cost >= 0.05:
        add_admin_alert(
            db,
            alert_type="cost_without_result",
            severity="warning",
            title="Náklad bez použitelného výsledku",
            message=(
                f"Úloha {job.operation or job.task_type} ({job.id}) skončila bez použitelného výsledku "
                f"po evidovaném nákladu ${total_cost:.4f}. Zdroj: {job.source}; projekt: {job.project_id or '—'}."
            ),
            dedupe_key=f"cost_without_result:{job.id}",
            metadata={
                "job_id": job.id, "operation": job.operation, "source": job.source,
                "project_id": job.project_id, "cost_usd": total_cost,
                "providers": [a.get("provider") for a in attempts_trace if a.get("provider")],
            },
        )
    _record_contract_trace(
        trace, contract, validated=False, repaired=False,
        validation_errors=last_errors[:20], provider_calls=actual_provider_calls,
    )
    decision.trace_json = json.dumps(trace, ensure_ascii=False)
    _clear_transient_payloads(job, s.store_payloads)
    db.commit()
    try:
        from app.accounts import settle_job
        settle_job(db, job)
    except Exception:
        pass
    return job

def _actual_cost(route, input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * route.input_usd_per_million
        + (output_tokens / 1_000_000) * route.output_usd_per_million,
        8,
    )


def is_queue_paused(db) -> bool:
    setting = db.get(RuntimeSetting, "queue_paused")
    return bool(setting and setting.value.lower() == "true")
