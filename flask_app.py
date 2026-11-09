from __future__ import annotations

import secrets
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
import os
import smtplib
import logging
import re
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, g, flash
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.engine import create_job, execute_job, fail_job_safely
from app.models import Job, JobAttempt, JobFeedback, ModelRoute, RuntimeSetting, RoutingDecision, CustomerAccount, Connector, CreditLedger, CreditOrder, JobBilling, ChatUsageUser, AccountEmailVerification, ExternalIdentity, ExternalOrder, AccountTrash, AccountInvitation, AccountAdminState, AdminAccountAudit, FreshKeyAppBinding
from app.routing_trace import inspect_routes
from app.diagnostics import report, VERSION
from app.costs import cost_report
from app.diagnostic_share import current as current_share, authenticated_share, generate as generate_share, refresh as refresh_share, revoke as revoke_share
from app.capabilities import CODE_RE, MEDIA_CAPABILITIES, definitions, normalize_phrases, assign_task, assign_with_context
from app.models import CapabilityDefinition
from app.multimodal import (
    available_capabilities, understand_image, image_generation_request_parameters,
    image_request_cost_estimate,
)
from app.providers import build_providers
from app.router import route_candidates
from app.routing_knowledge import profile_json_for_admin, normalize_route_profile
from app.schemas import EngineRequest, JobFeedbackRequest
from app.output_contracts import resolve_contract, contract_public_metadata
from app.seed import seed_defaults
from app.accounts import make_invitation, accept_invitation, admin_suspended
from app.admin_customers import initialize_trash, audit, trash_account, purge_trash
from app.account_finance import account_finance, accounts_finance_overview
from app.fx_cnb import refresh_cnb_fx, fx_status
from app.chat_usage import accept_chat_identity, chat_user_summary, sync_chat_users
from app.accounts import authenticate, create_account, create_connector, rotate_connector, delete_connector, find_connector, balance, create_order, mark_order_paid, bind_job, credit_price_czk, credit_setting, credits_per_usd, verification_token, verify_email_token, is_verified, password_reset_token, reset_password, manually_verify_account
from app.database_admin import test_connection, migrate_current, save_env_database
from app.mailer import smtp_config, send_email, save_env_secret
from app.admin_alerts import list_admin_alerts, dismiss_admin_alert, clear_admin_alerts
from app.provider_guard import all_provider_guards, clear_provider_guard
from app import fresh_account
from app.support import create_challenge, submit_support
from app.schema_upgrade import ensure_schema_upgrades
from werkzeug.security import check_password_hash

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR / "app"

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
    static_url_path="/static",
)
app.config["JSON_AS_ASCII"] = False
app.secret_key = get_settings().customer_session_secret
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)


def safe_json_loads(raw, default=None):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def init_database():
    Base.metadata.create_all(engine)
    ensure_schema_upgrades(engine)
    with SessionLocal() as db:
        seed_defaults(db)
        # Backward compatibility: accounts that existed before e-mail verification
        # are marked verified once. New registrations always create a pending row.
        existing = db.scalars(select(CustomerAccount)).all()
        for account in existing:
            # Admin-created accounts await a one-time e-mail invitation; never auto-verify them at restart.
            if db.get(AccountEmailVerification, account.id) is None and db.get(AccountInvitation, account.id) is None and account.active:
                db.add(AccountEmailVerification(account_id=account.id, token_hash='legacy-'+account.id.replace('-', '')[:25], expires_at=datetime.now(timezone.utc), sent_at=datetime.now(timezone.utc), verified_at=datetime.now(timezone.utc)))
        db.commit()
        initialize_trash(db)


init_database()
settings = get_settings()


@app.context_processor
def engine_version_context():
    return {'engine_version': VERSION}


@app.template_filter('prague_time')
def prague_time(value, fmt='%d.%m.%Y %H:%M:%S'):
    """MySQL DATETIME is stored in UTC without tzinfo; UI presents Europe/Prague."""
    if value is None: return '—'
    if isinstance(value,str):
        try: value=datetime.fromisoformat(value)
        except ValueError: return value
    if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo('Europe/Prague')).strftime(fmt)


@app.context_processor
def customer_support_context():
    if not (request.path == '/' or request.path.startswith('/account')):
        return {}
    support_email = ''
    account_id = session.get('customer_account_id')
    if account_id:
        with SessionLocal() as db:
            account = db.get(CustomerAccount, account_id)
            if account and account.active:
                support_email = account.email
    return {'support_form': create_challenge(), 'support_user_email': support_email}


@app.post('/account/support')
def customer_support_submit():
    # Only local destination paths can appear in the redirect; never accept URLs.
    destination = request.form.get('return_to', '/account/login')
    if not (destination == '/' or destination.startswith('/account')) or destination.startswith('//') or any(c in destination for c in '?\\#'):
        destination = '/account/login'
    try:
        account_id = session.get('customer_account_id')
        with SessionLocal() as db:
            ref = submit_support(db, request.form, account_id, request.remote_addr or 'unknown')
        flash('Děkujeme! Zprávu jsme přijali. Číslo požadavku: ' + ref, 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    except Exception:
        app.logger.exception('Customer support delivery failed')
        flash('Zprávu teď nelze odeslat. Zkus to prosím později nebo napiš na soufek@freshcrackers.cz.', 'error')
    return redirect(destination + ('?support=open' if destination != '/' else '?support=open'), code=303)


def customer_credit_activity(db, account_id, limit=30):
    from types import SimpleNamespace
    movement=db.scalars(select(CreditLedger).where(
        CreditLedger.account_id==account_id,
        CreditLedger.entry_type.notin_(['reserve','settlement'])
    ).order_by(CreditLedger.created_at.desc()).limit(limit)).all()
    bills=db.execute(select(JobBilling,Job).join(Job,Job.id==JobBilling.job_id)
        .where(JobBilling.account_id==account_id,Job.status=='completed',JobBilling.credits_charged>0)
        .order_by(Job.finished_at.desc()).limit(limit)).all()
    billed_ids={j.id for b,j in bills}
    movement=[e for e in movement if not (e.entry_type=='usage' and e.reference in billed_ids)]
    usage=[SimpleNamespace(created_at=j.finished_at or j.created_at,
             entry_type='usage',delta_credits=-float(b.credits_charged),
             note='Zpracování AI požadavku',reference=j.id) for b,j in bills]
    return sorted([*movement,*usage],key=lambda e:e.created_at or datetime.min,reverse=True)[:limit]


@app.template_filter('customer_ledger_type')
def customer_ledger_type(entry):
    return {'woocommerce_topup':'Dobití kreditů','topup':'Dobití kreditů',
            'reserve':'Rezervace kreditů','settlement':'Vyúčtování požadavku',
            'usage':'Čerpání kreditů','account_deletion':'Ukončení účtu',
            'refund':'Vrácení kreditů'}.get(entry, 'Pohyb kreditů')


@app.template_filter('customer_ledger_note')
def customer_ledger_note(note):
    if not note: return ''
    if note.startswith('WooCommerce · '): return 'Nákup v e-shopu Fresh Crackers'
    if any(word in note.lower() for word in ('woocommerce', 'webhook', 'api', 'debug', 'internal')):
        return 'Pohyb v zákaznickém účtu'
    return note


@app.template_filter('customer_order_status')
def customer_order_status(status):
    return {'pending':'Čeká na platbu','on-hold':'Čeká na platbu',
            'processing':'Platba přijata','completed':'Dokončeno',
            'failed':'Neúspěšná','cancelled':'Zrušeno','refunded':'Vráceno',
            'checkout-draft':'Rozpracováno'}.get(str(status or '').lower(), 'Ve zpracování')


@app.template_filter('customer_job_status')
def customer_job_status(status):
    return {'completed':'Dokončeno', 'failed':'Nepodařilo se', 'queued':'Čeká na zpracování',
            'running':'Zpracovává se','claimed':'Zpracovává se','cancelled':'Zrušeno'}.get(str(status or '').lower(), 'Zpracovává se')


def json_error(message: str, status: int):
    return jsonify({"detail": message}), status


def _aware_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def connector_expired(row):
    exp=_aware_utc(getattr(row,'expires_at',None))
    return bool(exp and exp<=datetime.now(timezone.utc))


def connector_status_label(row):
    if getattr(row,'deleted_at',None): return 'Odstraněný'
    if connector_expired(row): return 'Platnost vypršela'
    return 'Aktivní' if row.active else 'Vypnutý'


def connector_validity_label(row):
    exp=_aware_utc(getattr(row,'expires_at',None))
    return 'Trvale' if exp is None else exp.strftime('%d.%m.%Y')


def connector_expiry_from_form(form):
    mode=(form.get('validity') or 'permanent').strip().lower()
    now=datetime.now(timezone.utc)
    if mode=='1d': return now+timedelta(days=1)
    if mode=='30d': return now+timedelta(days=30)
    if mode=='permanent': return None
    if mode=='custom':
        raw=(form.get('expires_on') or '').strip()
        try:
            parsed=datetime.strptime(raw,'%Y-%m-%d').replace(hour=23,minute=59,second=59,tzinfo=timezone.utc)
        except ValueError:
            raise ValueError('Vyber platné datum konektoru.')
        if parsed<=now:
            raise ValueError('Datum platnosti musí být v budoucnosti.')
        return parsed
    raise ValueError('Neplatná volba platnosti konektoru.')


app.jinja_env.globals['connector_status_label']=connector_status_label
app.jinja_env.globals['connector_validity_label']=connector_validity_label


def require_api_key(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return json_error("Missing Bearer API key", 401)
        key = auth.split(" ", 1)[1].strip()
        g.connector = None
        g.trusted_chat = bool(settings.fresh_chat_engine_key and secrets.compare_digest(key, settings.fresh_chat_engine_key))
        if g.trusted_chat or any(secrets.compare_digest(key, allowed) for allowed in settings.api_keys):
            return view(*args, **kwargs)
        with SessionLocal() as db:
            connector = find_connector(db, key)
            if not connector or admin_suspended(db,connector.account_id) or not (db.get(CustomerAccount,connector.account_id) and db.get(CustomerAccount,connector.account_id).active):
                return json_error("Invalid API key", 401)
            if balance(db, connector.account_id) <= 0:
                return json_error("Insufficient credits", 402)
            g.connector = {"id": connector.id, "account_id": connector.account_id, "name": connector.name}
        return view(*args, **kwargs)
    return wrapped


def require_fresh_key_api_key(view):
    """Authenticate a Fresh app and bind its API key to one stable Fresh Key app namespace."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return json_error("Missing Bearer API key", 401)
        key = auth.split(" ", 1)[1].strip()
        requested_app = str(request.headers.get("X-Fresh-App") or "").strip().lower()
        valid_app = bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", requested_app))
        g.connector = None
        g.fresh_key_app_code = ""
        g.trusted_chat = bool(settings.fresh_chat_engine_key and secrets.compare_digest(key, settings.fresh_chat_engine_key))
        if g.trusted_chat or any(secrets.compare_digest(key, allowed) for allowed in settings.api_keys):
            if not valid_app:
                return json_error("Fresh Key requires X-Fresh-App", 400)
            g.fresh_key_app_code = requested_app
            return view(*args, **kwargs)
        with SessionLocal() as db:
            connector = find_connector(db, key)
            if not connector or admin_suspended(db, connector.account_id) or not (db.get(CustomerAccount, connector.account_id) and db.get(CustomerAccount, connector.account_id).active):
                return json_error("Invalid API key", 401)
            binding = db.scalar(select(FreshKeyAppBinding).where(FreshKeyAppBinding.connector_id == connector.id))
            if binding:
                if valid_app and requested_app != binding.app_code:
                    return json_error("This Engine API key is bound to another Fresh Key app", 403)
                app_code = binding.app_code
            else:
                if not valid_app:
                    return json_error("Fresh Key requires X-Fresh-App on first use", 400)
                binding = FreshKeyAppBinding(connector_id=connector.id, app_code=requested_app)
                db.add(binding)
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    binding = db.scalar(select(FreshKeyAppBinding).where(FreshKeyAppBinding.connector_id == connector.id))
                    if not binding or binding.app_code != requested_app:
                        return json_error("Fresh Key app binding conflict", 409)
                app_code = requested_app
            g.connector = {"id": connector.id, "account_id": connector.account_id, "name": connector.name}
            g.fresh_key_app_code = app_code
        return view(*args, **kwargs)
    return wrapped


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        ok = bool(
            auth
            and secrets.compare_digest(auth.username or "", settings.admin_user)
            and secrets.compare_digest(auth.password or "", settings.admin_password)
        )
        if not ok:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Fresh AI Engine"'},
            )
        return view(*args, **kwargs)

    return wrapped


def parse_engine_request():
    try:
        return EngineRequest.model_validate(request.get_json(force=True, silent=False) or {})
    except ValidationError as exc:
        return json_error("Validation error: " + str(exc), 422)
    except Exception as exc:
        return json_error("Invalid JSON: " + str(exc), 400)


def response_for_job(job, output=None, result=None, billing=None):
    canonical = output if output is not None else job.response_payload
    normalized = result
    if normalized is None and canonical is not None:
        if getattr(job, "result_type", None) == "json":
            try:
                normalized = json.loads(canonical)
            except Exception:
                normalized = None
        elif getattr(job, "result_type", None) != "image":
            normalized = canonical
    trace = {}
    try:
        if job.routing_decision and job.routing_decision.trace_json:
            trace = json.loads(job.routing_decision.trace_json)
    except Exception:
        trace = {}
    error_category = None
    if job.error:
        error_category = str(job.error).split(":", 1)[0][:80]
    billing = billing or getattr(job, "_fresh_billing", None) or {}
    cost_source = ((trace.get("media_asset") or {}).get("cost_source") or
                   ("route_token_tariff" if job.estimated_cost_usd else None))
    return {
        "run_id": job.id,
        "job_id": job.id,  # backward compatibility
        "status": job.status,
        "task_type": job.task_type,
        "operation": job.operation,
        "provider": job.provider,
        "model": job.model_name,
        "output": canonical,  # legacy canonical string; image jobs intentionally keep this null
        "result_type": getattr(job, "result_type", None),
        "result": normalized,
        "result_validated": bool(getattr(job, "result_validated", False)),
        "schema_id": getattr(job, "result_schema_id", None),
        "input_tokens": job.input_tokens,
        "output_tokens": job.output_tokens,
        "estimated_cost_usd": job.estimated_cost_usd,
        "cost": {
            "provider_usd": float(job.estimated_cost_usd or 0.0),
            "source": cost_source,
        },
        "credits": {
            "billing_applied": bool(billing.get("billing_applied")),
            "reserved": float(billing.get("reserved_credits") or 0.0),
            "charged": float(billing.get("credits_charged") or 0.0),
        },
        "latency_ms": job.latency_ms,
        "error_category": error_category,
        "error": job.error,
        "fallback_used": bool(trace.get("fallback_used")),
        "initial_provider": trace.get("initial_selected_provider"),
        "final_provider": trace.get("final_selected_provider") or job.provider,
        "recovery_trigger": trace.get("recovery_trigger"),
        "provider_calls": int(trace.get("provider_calls") or 0),
    }


def routing_context_for_payload(payload):
    ctx = dict(payload.context or {})
    if payload.input_data:
        ctx["_engine_input_data"] = payload.input_data
    declared = payload.output_contract.model_dump(by_alias=True, exclude_none=True) if payload.output_contract else None
    contract = resolve_contract(payload.operation, declared)
    if contract:
        ctx["_engine_output_contract"] = contract
    return ctx, contract


@app.get("/")
def root():
    """Public product homepage; signed-in customers go straight to their account."""
    if session.get("customer_account_id"):
        return redirect("/account")
    return render_template("landing.html")


@app.get("/v1/health")
def health():
    media = available_capabilities()
    return jsonify({
        "ok": True,
        "service": "fresh-ai-engine",
        "version": VERSION + "-cpanel-wsgi",
        "router": "2.3.0-media-dispatch",
        "default_quality": "auto",
        "structured_recovery": True,
        "provider_failover": True,
        "provider_guard": True,
        "image_generation": bool(media.get("image_generation", {}).get("openai")),
        "image_understanding": bool(media.get("image_understanding", {}).get("openai")),
    })


@app.get("/docs")
def docs():
    """Documentation entrypoint for signed-in account users and administrators."""
    if session.get("customer_account_id"):
        return redirect("/account/developer-api", code=302)
    return redirect("/admin/developer-api", code=302)


@app.get("/admin/developer-api")
@require_admin
def developer_api_page():
    """Documentation of verified existing interfaces, without revealing secrets."""
    with SessionLocal() as db:
        try:
            available = [(c.code, c.label, bool(c.enabled)) for c in definitions(db)]
        except Exception:
            available = []
    return render_template("developer_api.html", capabilities=available, admin_view=True,
                           api_spec_url="/admin/developer-api/openapi.json")


@app.get("/admin/developer-api/openapi.json")
@require_admin
def developer_api_spec():
    """Authenticated machine-readable contract matching existing Flask routes."""
    from app.developer_api import build_openapi
    return jsonify(build_openapi())


@app.post('/v1/fresh-account/order')
def fresh_account_order_event():
    app_id=request.headers.get('X-Fresh-App-Id','')
    shared=request.headers.get('X-Fresh-App-Secret','')
    if not fresh_account.configured() or app_id!=fresh_account.app_id() or not secrets.compare_digest(shared,fresh_account.secret()):
        return json_error('Unauthorized',401)
    data=request.get_json(silent=True) or {}
    if data.get('status') not in {'processing','completed'}: return jsonify({'ok':True,'ignored':True})
    external_user_id=str(data.get('user_id') or '')
    external_order_id=str(data.get('order_id') or '')
    event_id='wc:'+external_order_id+':'+str(data.get('action_type') or 'credit')
    if not external_user_id or not external_order_id: return json_error('Missing order identity',422)
    with SessionLocal() as db:
        ident=db.scalar(select(ExternalIdentity).where(ExternalIdentity.external_user_id==external_user_id))
        if not ident: return json_error('Account is not linked to Fresh AI Engine',404)
        existing=db.get(ExternalOrder,event_id)
        if existing: return jsonify({'ok':True,'duplicate':True,'order_id':external_order_id})
        action_type=str(data.get('action_type') or 'credit')[:40]
        value=float(data.get('action_value') or 0)
        row=ExternalOrder(id=event_id,account_id=ident.account_id,provider='woocommerce',external_order_id=external_order_id,
            product_code=str(data.get('product_code') or '')[:100],action_type=action_type,action_value=value,
            amount=float(data.get('amount') or 0),currency=str(data.get('currency') or 'CZK')[:12],status='completed')
        db.add(row)
        if action_type=='credit' and value>0:
            db.add(CreditLedger(account_id=ident.account_id,delta_credits=value,entry_type='woocommerce_topup',reference=external_order_id,note='WooCommerce · '+str(data.get('product_name') or 'kredit')[:180]))
        db.commit()
    return jsonify({'ok':True,'order_id':external_order_id,'action':action_type,'value':value})

@app.post("/v1/engine/route")
@require_api_key
def preview_route():
    payload = parse_engine_request()
    if isinstance(payload, tuple):
        return payload
    with SessionLocal() as db:
        route_context, contract = routing_context_for_payload(payload)
        resolved_task, assignment = assign_with_context(db, payload.prompt, payload.task_type, payload.context)
        trace = inspect_routes(
            db, resolved_task, payload.quality, payload.prompt, payload.max_output_tokens,
            payload.max_cost_usd, context=route_context, operation=payload.operation
        )
        candidates = route_candidates(
            db,
            resolved_task,
            payload.quality,
            payload.prompt,
            payload.max_output_tokens,
            payload.max_cost_usd, context=route_context, operation=payload.operation,
        )
        return jsonify(
            {
                "task_assignment": assignment,
                "operation": payload.operation,
                "routing_mode": trace.get("routing_mode"),
                "execution_profile": trace.get("execution_profile"),
                "selection_reason": trace.get("selection_reason"),
                "selected_route": trace.get("selected_route"),
                "output_contract": contract_public_metadata(contract),
                "candidates": [
                    {
                        "code": c.route.code,
                        "provider": c.route.provider,
                        "model": c.route.model_name,
                        "score": round(c.score, 2),
                        "estimated_cost_usd": c.estimated_cost_usd,
                    }
                    for c in candidates
                ]
            }
        )


@app.post("/v1/engine/explain")
@require_api_key
def explain_route():
    payload = parse_engine_request()
    if isinstance(payload, tuple): return payload
    with SessionLocal() as db:
        route_context, contract = routing_context_for_payload(payload)
        resolved_task, assignment = assign_with_context(db, payload.prompt, payload.task_type, payload.context)
        trace = inspect_routes(db, resolved_task, payload.quality, payload.prompt,
                               payload.max_output_tokens, payload.max_cost_usd, context=route_context, operation=payload.operation)
        trace['task_assignment'] = assignment
        trace['operation'] = payload.operation
        trace['output_contract'] = contract_public_metadata(contract)
        return jsonify(trace)


@app.get("/v1/engine/jobs/<job_id>/decision")
@require_api_key
def job_decision(job_id):
    with SessionLocal() as db:
        item = db.get(RoutingDecision, job_id)
        return jsonify(safe_json_loads(item.trace_json, {"error":"routing_trace_unavailable"})) if item else json_error("Decision not found", 404)


@app.get("/admin/routing-decisions")
@require_admin
def routing_decisions_page():
    with SessionLocal() as db:
        routes = db.scalars(select(ModelRoute).order_by(ModelRoute.priority.desc())).all()
        jobs = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(50)).all()
        return render_template('routing_decisions.html', routes=routes, jobs=jobs,
                               capabilities=definitions(db),
                               availability={k:v.available() for k,v in build_providers().items()})


@app.after_request
def secure_diagnostics_response(response):
    if request.path.startswith('/diagnostics/') or request.path.startswith('/admin/diagnostics'):
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        # The admin page contains POST forms. `no-referrer` suppressed their
        # same-origin Referer; browsers that omit Origin then failed the
        # origin check below with HTTP 403. Public bearer-token links must
        # still never leak their query string in a Referer header.
        response.headers['Referrer-Policy'] = (
            'same-origin' if request.path.startswith('/admin/diagnostics')
            else 'no-referrer'
        )
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def admin_post_allowed():
    # HTTP Basic auth is automatically attached by browsers: defend sensitive
    # admin POSTs from cross-site submissions. Reject missing Origin/Referer.
    origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
    parsed = urlsplit(origin)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    # Do not trust arbitrary X-Forwarded-Host headers from the internet. The
    # canonical external origin is explicitly configured by the operator.
    # This handles HTTPS clients behind an internal HTTP Passenger proxy.
    configured = os.getenv('FRESH_PUBLIC_ORIGIN', 'https://aiengine.freshcrackers.cz')
    candidates = [urlsplit(configured), urlsplit(request.host_url)]
    for expected in candidates:
        if (parsed.hostname == expected.hostname and parsed.port == expected.port and
            (parsed.scheme == expected.scheme or
             (expected.scheme == 'http' and parsed.scheme == 'https'))):
            return True
    return False


def diagnostics_context(db, token=None, notice=None, error=False):
    # The diagnostics page must remain accessible when collecting the manifest
    # fails, so administrators can rotate/revoke an already issued share.
    share = current_share(db)
    if share and token and not authenticated_share(db, token):
        token = None
    try:
        manifest = report(db)
    except Exception as exc:
        app.logger.error('Diagnostic manifest collection failed: %s', type(exc).__name__)
        manifest = {'engine_version': VERSION,
                    'runtime': {'python': 'nedostupné', 'database_driver': 'nedostupná'},
                    'files': [], 'recent_jobs': []}
        if not notice:
            notice = ('Nepodařilo se sestavit diagnostický přehled (' +
                      type(exc).__name__ + '). Podrobnosti najdeš v serverovém logu.')
            error = True
    # A reverse proxy may expose its internal scheme/host via request.url_root.
    # Build public links from the operator-configured canonical HTTPS origin.
    base=os.getenv('FRESH_PUBLIC_ORIGIN', 'https://aiengine.freshcrackers.cz').rstrip('/')
    return dict(report=manifest, share=share, token=token,
                manifest_url=(base + '/diagnostics/manifest.json?key=' + token) if token else '',
                bundle_url=(base + '/diagnostics/support.zip?key=' + token) if token else '',
                notice=notice, error=error)


@app.get('/admin/diagnostics')
@require_admin
def diagnostics_page():
    with SessionLocal() as db:
        return render_template('diagnostics.html', **diagnostics_context(db))


@app.post('/admin/diagnostics/generate')
@require_admin
def diagnostics_generate():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.', 403)
    with SessionLocal() as db:
        try:
            token, _ = generate_share(db)
            return render_template('diagnostics.html', **diagnostics_context(
                db, token=token, notice='Vytvořen nový odkaz. Předchozí klíč je zneplatněn.'))
        except Exception as exc:
            app.logger.error('Diagnostic generation failed: %s: %s', type(exc).__name__, str(exc)[:180])
            return render_template('diagnostics.html', **diagnostics_context(
                db, notice='Diagnostiku se nepodařilo vytvořit (' + type(exc).__name__ + '). Původní odkaz zůstal beze změny. Podrobnosti jsou v serverovém logu.', error=True)), 500


@app.post('/admin/diagnostics/refresh')
@require_admin
def diagnostics_refresh():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.', 403)
    with SessionLocal() as db:
        try:
            token = (request.form.get('current_token') or '').strip()
            if token and authenticated_share(db, token):
                share = refresh_share(db)
                return render_template('diagnostics.html', **diagnostics_context(
                    db, token=token, notice='Manifest aktualizován. Odkaz i původní čas vypršení zůstaly zachovány.'))
            # Browser no longer knows the bearer token (for example after a revisit).
            # Rotate it instead of producing a refreshed snapshot the admin cannot open.
            token, _ = generate_share(db)
            return render_template('diagnostics.html', **diagnostics_context(
                db, token=token, notice='Manifest aktualizován a vytvořen nový 24hodinový odkaz.'))
        except Exception as exc:
            app.logger.error('Diagnostic refresh failed: %s: %s', type(exc).__name__, str(exc)[:180])
            return render_template('diagnostics.html', **diagnostics_context(
                db, notice='Aktualizace selhala (' + type(exc).__name__ + '); předchozí diagnostika zůstává dostupná.', error=True)), 500


@app.post('/admin/diagnostics/revoke')
@require_admin
def diagnostics_revoke():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.', 403)
    with SessionLocal() as db:
        revoke_share(db)
        return render_template('diagnostics.html', **diagnostics_context(
            db, notice='Odkaz byl zneplatněn.'))


@app.get('/diagnostics/share/<token>/manifest.json')
def shared_manifest(token):
    with SessionLocal() as db:
        share = authenticated_share(db, token)
        if not share:
            return json_error('Odkaz je neplatný nebo vypršel.', 404)
        response = Response(share.manifest_json, mimetype='application/json; charset=utf-8')
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response


@app.get('/diagnostics/share/<token>/support.zip')
def shared_bundle(token):
    with SessionLocal() as db:
        share = authenticated_share(db, token)
        if not share:
            return json_error('Odkaz je neplatný nebo vypršel.', 404)
        # Passenger's wsgi.file_wrapper may call fileno() on BytesIO and fail
        # with io.UnsupportedOperation. Send the already materialized bytes as a
        # plain Flask Response instead; this is portable across Passenger/cPanel.
        payload = bytes(share.bundle_zip or b'')
        if not payload.startswith(b'PK'):
            return json_error('Diagnostický ZIP není dostupný. Obnov diagnostický odkaz.', 500)
        response = Response(payload, status=200, mimetype='application/zip')
        response.headers['Content-Disposition'] = 'attachment; filename="fresh-ai-engine-diagnostics.zip"'
        response.headers['Content-Length'] = str(len(payload))
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response


@app.get('/diagnostics/manifest.json')
def shared_manifest_query():
    token=(request.args.get('key') or '').strip()
    return shared_manifest(token)

@app.get('/diagnostics/support.zip')
def shared_bundle_query():
    token=(request.args.get('key') or '').strip()
    return shared_bundle(token)


@app.get('/admin/capabilities')
@require_admin
def admin_capabilities():
    with SessionLocal() as db:
        return render_template('capabilities.html', capabilities=definitions(db),
                               phrases={c.code: normalize_phrases(c.phrases_json) for c in definitions(db)},
                               media_codes=MEDIA_CAPABILITIES)


def capability_fields(form):
    label = (form.get('label') or '').strip()
    description = (form.get('description') or '').strip()
    if not label or len(label)>120 or not description or len(description)>2000:
        raise ValueError('Vyplň název a definici (max. 120 / 2000 znaků).')
    priority = int(form.get('priority', 50))
    if not 0 <= priority <= 1000:
        raise ValueError('Priorita musí být 0–1000.')
    phrases = normalize_phrases(form.get('phrases',''))
    return label, description, priority, json.dumps(phrases, ensure_ascii=False)


def capability_error(db, message, status=400):
    return render_template('capabilities.html', capabilities=definitions(db),
                           phrases={c.code: normalize_phrases(c.phrases_json) for c in definitions(db)},
                           media_codes=MEDIA_CAPABILITIES, notice=message, error=True), status


@app.post('/admin/capabilities/create')
@require_admin
def admin_create_capability():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.', 403)
    with SessionLocal() as db:
        try:
            code = (request.form.get('code') or '').strip()
            if not CODE_RE.fullmatch(code) or code in MEDIA_CAPABILITIES:
                raise ValueError('Zadej platný, dosud nepoužitý kód textové schopnosti.')
            if db.get(CapabilityDefinition, code): raise ValueError('Tento kód již existuje.')
            label, description, priority, phrases = capability_fields(request.form)
            db.add(CapabilityDefinition(code=code,label=label,description=description,
                                        priority=priority, phrases_json=phrases))
            db.commit()
        except (ValueError, TypeError) as exc:
            return capability_error(db, str(exc))
    return redirect('/admin/capabilities', code=303)


@app.post('/admin/capabilities/<code>/update')
@require_admin
def admin_update_capability(code):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.', 403)
    with SessionLocal() as db:
        row = db.get(CapabilityDefinition, code)
        if not row: return json_error('Schopnost neexistuje.', 404)
        try:
            label, description, priority, phrases = capability_fields(request.form)
            row.label=label
            row.description=description
            row.priority=priority
            row.phrases_json=phrases
            row.enabled=request.form.get('enabled') == 'on' and code not in MEDIA_CAPABILITIES
            db.commit()
        except (ValueError, TypeError) as exc:
            return capability_error(db, str(exc))
    return redirect('/admin/capabilities', code=303)


@app.get('/v1/media/capabilities')
@require_api_key
def media_capabilities():
    return jsonify(available_capabilities())


@app.post('/v1/media/image/generate')
@require_api_key
def image_generate():
    """Compatibility endpoint backed by the exact same Engine image job path."""
    body = request.get_json(silent=True) or {}
    try:
        input_data = {k: body.get(k) for k in (
            'model','size','width','height','image_quality','output_format','format',
            'background','output_compression'
        ) if body.get(k) is not None}
        # Historically this endpoint used `quality` for image quality. Preserve
        # that behavior while exposing `engine_quality` for account policy.
        raw_quality = body.get('quality')
        engine_quality = body.get('engine_quality') or 'balanced'
        if raw_quality is not None:
            rq = str(raw_quality).strip().lower()
            if rq in {'low','medium','high','xhigh','max','auto'}:
                input_data['quality'] = rq
            elif rq in {'economy','balanced','best'} and not body.get('engine_quality'):
                engine_quality = rq
            else:
                input_data['quality'] = rq
        payload = EngineRequest.model_validate({
            'source': body.get('source') or 'media-api',
            'project_id': body.get('project_id'),
            'user_id': body.get('user_id'),
            'task_type': 'image_generation',
            'operation': 'image.generate',
            'quality': engine_quality,
            'prompt': body.get('prompt'),
            'input_data': input_data,
            'max_cost_usd': body.get('max_cost_usd'),
        })
    except ValidationError as exc:
        return json_error('Validation error: ' + str(exc), 422)
    except Exception as exc:
        return json_error('Invalid image request: ' + str(exc), 400)
    return execute_payload_sync(payload)


@app.post('/v1/media/image/understand')
@require_api_key
def image_understand():
    if getattr(g, 'connector', None): return json_error('Customer media billing is not enabled yet.', 501)
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(understand_image(body.get('prompt'),body.get('image_base64'),
                                      body.get('mime_type','image/jpeg'),body.get('model')))
    except ValueError as exc: return json_error(str(exc),400)
    except Exception: return json_error('Image understanding failed; inspect provider configuration and server logs.',502)


def customer_required_reserve(db, payload):
    """Estimate a temporary customer reservation without making routing a hard dependency.

    If Router 2.3 history/schema diagnostics are temporarily unavailable, reserve a
    conservative amount and let the actual job settle to real charged credits. The
    reservation itself is hidden from the customer-facing activity view.
    """
    if not getattr(g, "connector", None):
        return 0.0
    if payload.operation in {"image.generate", "image_generation.generate"}:
        try:
            params = image_generation_request_parameters(payload.input_data or {}, payload.quality)
            reserve_usd = image_request_cost_estimate(params["model"], params["quality"], params["size"])
            # Small safety margin protects the account because actual media cost
            # is settled from provider usage after completion.
            reserve_usd = max(0.01, reserve_usd * 1.15)
            if payload.max_cost_usd is not None:
                reserve_usd = min(reserve_usd, float(payload.max_cost_usd))
            return round(reserve_usd * credits_per_usd(db), 6)
        except Exception:
            fallback_usd = min(float(payload.max_cost_usd) if payload.max_cost_usd is not None else 0.15, 1.0)
            return round(max(0.01, fallback_usd) * credits_per_usd(db), 6)
    try:
        route_context, contract = routing_context_for_payload(payload)
        resolved_task,_ = assign_with_context(db, payload.prompt, payload.task_type, payload.context)
        candidates = route_candidates(
            db, resolved_task, payload.quality, payload.prompt, payload.max_output_tokens,
            payload.max_cost_usd, context=route_context, operation=payload.operation,
        )
        if candidates:
            count = 2 if contract and contract.get("type") == "json" else 1
            reserve_usd = sum(float(c.estimated_cost_usd or 0) for c in candidates[:count])
            if payload.max_cost_usd is not None:
                reserve_usd = min(reserve_usd, float(payload.max_cost_usd))
            return round(reserve_usd * credits_per_usd(db), 6)
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        app.logger.warning('Router reserve estimate unavailable: %s', type(exc).__name__)
        payload.context['_engine_reserve_fallback'] = type(exc).__name__

    # Prefer the caller's hard ceiling when supplied; otherwise use a small
    # conservative text-request reserve. Settlement always corrects it afterwards.
    fallback_usd = float(payload.max_cost_usd) if payload.max_cost_usd is not None else 0.05
    fallback_usd = max(0.0, min(fallback_usd, 5.0))
    return round(fallback_usd * credits_per_usd(db), 6)


def execute_payload_sync(payload: EngineRequest):
    """Shared synchronous Engine execution for /run and media compatibility endpoints."""
    if payload.operation in {"image.understand", "image_understand"} and getattr(g, "connector", None):
        return json_error("Customer image-understanding billing is not enabled yet.", 501)
    with SessionLocal() as db:
        if getattr(g, "connector", None):
            payload.source = g.connector["name"]
            payload.user_id = payload.user_id or g.connector["account_id"]
        reserve = customer_required_reserve(db, payload)
        if getattr(g, "connector", None) and balance(db, g.connector["account_id"]) + 1e-9 < reserve:
            return json_error("Insufficient credits for this request", 402)
        # Untrusted generic/customer API keys may never supply private Chat memories.
        if not getattr(g, 'trusted_chat', False):
            payload.context.pop('memory_candidates', None)
            payload.context.pop('memory_operation', None)
            payload.context.pop('_chat_memory_verified', None)
            payload.context.pop('chat_identity', None)
            # The reserved reporting namespace cannot be claimed by a generic key.
            if payload.source == 'fresh-chat' and not getattr(g, 'connector', None):
                payload.source = 'external-api'
        else:
            payload.source = 'fresh-chat'
            payload.context['_chat_memory_verified'] = True
            accept_chat_identity(db, payload)
        job = create_job(db, payload)
        if getattr(g, "connector", None):
            connector = db.get(Connector, g.connector["id"])
            bind_job(db, job.id, connector, reserve)
        try:
            job = execute_job(db, job)
        except Exception as exc:
            app.logger.exception('Fresh AI Engine unexpected execution failure for job %s', job.id)
            job = fail_job_safely(db, job, 'execution_internal_error', exc, {'provider_calls': 0})
        output = getattr(job, "_fresh_output", None)
        if output is None:
            output = job.response_payload
        result = getattr(job, "_fresh_result", None)
        bill = db.get(JobBilling, job.id)
        billing = {
            "billing_applied": bool(bill),
            "reserved_credits": float(bill.reserved_credits or 0) if bill else 0.0,
            "credits_charged": float(bill.credits_charged or 0) if bill else 0.0,
            "cost_usd": float(bill.cost_usd or 0) if bill else float(job.estimated_cost_usd or 0),
        }
        return jsonify(response_for_job(job, output, result, billing))


@app.post("/v1/engine/run")
@require_api_key
def run_engine():
    payload = parse_engine_request()
    if isinstance(payload, tuple):
        return payload
    return execute_payload_sync(payload)


@app.post("/v1/engine/jobs")
@require_api_key
def queue_job():
    payload = parse_engine_request()
    if isinstance(payload, tuple):
        return payload
    if payload.operation in {"image.generate", "image_generation.generate", "image.understand", "image_understand"}:
        return json_error("Media operations currently require synchronous /v1/engine/run.", 422)
    with SessionLocal() as db:
        if getattr(g, "connector", None):
            payload.source = g.connector["name"]
            payload.user_id = payload.user_id or g.connector["account_id"]
        reserve = customer_required_reserve(db, payload)
        if getattr(g, "connector", None) and balance(db, g.connector["account_id"])+1e-9 < reserve:
            return json_error("Insufficient credits for this request", 402)
        if not getattr(g, 'trusted_chat', False):
            payload.context.pop('memory_candidates', None)
            payload.context.pop('memory_operation', None)
            payload.context.pop('_chat_memory_verified', None)
            payload.context.pop('chat_identity', None)
            # The reserved reporting namespace cannot be claimed by a generic key.
            if payload.source == 'fresh-chat' and not getattr(g, 'connector', None):
                payload.source = 'external-api'
        else:
            payload.source = 'fresh-chat'
            payload.context['_chat_memory_verified'] = True
            accept_chat_identity(db, payload)
        job = create_job(db, payload)
        if getattr(g, "connector", None):
            connector = db.get(Connector, g.connector["id"])
            bind_job(db, job.id, connector, reserve)
        return jsonify({"job_id": job.id, "status": job.status}), 201


@app.get("/v1/engine/jobs/<job_id>")
@require_api_key
def job_status(job_id: str):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return json_error("Job not found", 404)
        if getattr(g, "connector", None):
            bill=db.get(JobBilling,job_id)
            if not bill or bill.account_id!=g.connector["account_id"]:
                return json_error("Job not found", 404)
        return jsonify(response_for_job(job))


@app.post("/v1/engine/jobs/<job_id>/feedback")
@require_api_key
def job_feedback(job_id: str):
    try:
        payload = JobFeedbackRequest.model_validate(request.get_json(force=True, silent=False) or {})
    except ValidationError as exc:
        return json_error("Validation error: " + str(exc), 422)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return json_error("Job not found", 404)
        if getattr(g, "connector", None):
            bill = db.get(JobBilling, job_id)
            if not bill or bill.account_id != g.connector["account_id"]:
                return json_error("Job not found", 404)
        row = db.get(JobFeedback, job_id)
        if row is None:
            row = JobFeedback(job_id=job_id, outcome=payload.outcome)
            db.add(row)
        else:
            row.outcome = payload.outcome
            row.created_at = datetime.now(timezone.utc)
        db.commit()
        return jsonify({"ok": True, "job_id": job_id, "outcome": payload.outcome})


@app.get("/admin")
@require_admin
def admin_dashboard():
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with SessionLocal() as db:
        total = db.scalar(select(func.count(Job.id))) or 0
        last24 = db.scalar(select(func.count(Job.id)).where(Job.created_at >= since)) or 0
        completed = db.scalar(select(func.count(Job.id)).where(Job.status == "completed")) or 0
        failed = db.scalar(select(func.count(Job.id)).where(Job.status == "failed")) or 0
        avg_latency = db.scalar(select(func.avg(Job.latency_ms)).where(Job.status == "completed")) or 0
        cost = db.scalar(select(func.sum(Job.estimated_cost_usd))) or 0
        queued = db.scalar(select(func.count(Job.id)).where(Job.status == "queued")) or 0
        recent = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(20)).all()
        setting = db.get(RuntimeSetting, "queue_paused")
        paused = bool(setting and setting.value == "true")
        alerts = list_admin_alerts(db)[:8]
        provider_guards = all_provider_guards(db, ["openai", "anthropic", "deepseek", "gemini"])
        return render_template(
            "dashboard.html",
            total=total,
            last24=last24,
            completed=completed,
            failed=failed,
            avg_latency=int(avg_latency),
            cost=float(cost),
            queued=queued,
            recent=recent,
            paused=paused,
            alerts=alerts,
            provider_guards=provider_guards,
        )


@app.post("/admin/alerts/<alert_id>/dismiss")
@require_admin
def admin_alert_dismiss(alert_id):
    if not admin_post_allowed():
        return json_error("Nedůvěryhodný původ formuláře.", 403)
    with SessionLocal() as db:
        dismiss_admin_alert(db, alert_id)
        db.commit()
    return redirect("/admin", code=303)


@app.post("/admin/alerts/clear")
@require_admin
def admin_alert_clear():
    if not admin_post_allowed():
        return json_error("Nedůvěryhodný původ formuláře.", 403)
    with SessionLocal() as db:
        clear_admin_alerts(db)
        db.commit()
    return redirect("/admin", code=303)


@app.post("/admin/provider-guard/<provider>/clear")
@require_admin
def admin_provider_guard_clear(provider):
    if not admin_post_allowed():
        return json_error("Nedůvěryhodný původ formuláře.", 403)
    if provider not in {"openai", "anthropic", "deepseek", "gemini"}:
        return json_error("Neznámý provider.", 404)
    with SessionLocal() as db:
        clear_provider_guard(db, provider)
        db.commit()
    return redirect("/admin", code=303)


@app.get("/admin/costs")
@require_admin
def admin_costs():
    period = (request.args.get("period", "30d") or "30d")[:20]
    source = (request.args.get("source", "") or "")[:80]
    project_arg = request.args.get("project")
    if request.args.get("project_missing") == "1" or project_arg == "__missing__":
        project = ""
    elif project_arg:
        project = project_arg[:120]
    else:
        project = None
    user_id = (request.args.get("user_id", "") or "")[:120]
    provider = (request.args.get("provider", "") or "")[:40]
    status = (request.args.get("status", "") or "")[:20]
    date_from = (request.args.get("date_from", "") or "")[:10]
    date_to = (request.args.get("date_to", "") or "")[:10]
    with SessionLocal() as db:
        return render_template("costs.html", **cost_report(
            db, period, source, project, user_id, provider, status, date_from, date_to
        ))


@app.get("/admin/jobs")
@require_admin
def admin_jobs():
    with SessionLocal() as db:
        rows = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(200)).all()
        return render_template("jobs.html", jobs=rows)


@app.get("/admin/jobs/<job_id>")
@require_admin
def admin_job_detail(job_id: str):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        attempts = (
            db.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.id)).all()
            if job
            else []
        )
        return render_template("job_detail.html", job=job, attempts=attempts, trace=safe_json_loads(job.routing_decision.trace_json, None) if job and job.routing_decision else None)


@app.post("/admin/jobs/<job_id>/cancel")
@require_admin
def admin_cancel_job(job_id: str):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job and job.status == "queued":
            job.status = "cancelled"
            db.commit()
            try:
                from app.accounts import settle_job
                settle_job(db, job)
            except Exception:
                pass
    return redirect("/admin/jobs", code=303)


@app.get("/admin/routing-analytics")
@require_admin
def admin_routing_analytics():
    period = request.args.get("period", "7d")
    if period not in {"24h", "7d", "30d", "all"}:
        period = "7d"
    since = _period_since(period)

    with SessionLocal() as db:
        available_text_types = [c.code for c in definitions(db) if c.code not in MEDIA_CAPABILITIES]
        completed_base = select(Job).where(Job.status == "completed")
        completed_stmt = _apply_since(completed_base, since)
        completed_jobs = db.scalars(completed_stmt.order_by(Job.created_at.desc())).all()

        total_completed = len(completed_jobs)
        total_cost = sum(float(j.estimated_cost_usd or 0) for j in completed_jobs)

        provider_map = {}
        task_map = {}
        source_map = {}
        route_map = {}
        matrix = {task: {} for task in available_text_types}

        for j in completed_jobs:
            provider = j.provider or "unknown"
            p = provider_map.setdefault(provider, {
                "provider": provider, "count": 0, "input_tokens": 0, "output_tokens": 0,
                "cost": 0.0, "latency_total": 0, "models": {},
            })
            p["count"] += 1
            p["input_tokens"] += int(j.input_tokens or 0)
            p["output_tokens"] += int(j.output_tokens or 0)
            p["cost"] += float(j.estimated_cost_usd or 0)
            p["latency_total"] += int(j.latency_ms or 0)
            model_name = j.model_name or "—"
            p["models"][model_name] = p["models"].get(model_name, 0) + 1

            task = j.task_type or "general"
            t = task_map.setdefault(task, {"task": task, "count": 0, "cost": 0.0, "latency_total": 0})
            t["count"] += 1
            t["cost"] += float(j.estimated_cost_usd or 0)
            t["latency_total"] += int(j.latency_ms or 0)
            matrix.setdefault(task, {})[provider] = matrix.setdefault(task, {}).get(provider, 0) + 1

            source = j.source or "unknown"
            source_map[source] = source_map.get(source, 0) + 1

            route_code = j.selected_route_code or "—"
            r = route_map.setdefault(route_code, {
                "route": route_code, "provider": provider, "model": model_name, "count": 0,
                "cost": 0.0, "latency_total": 0,
            })
            r["count"] += 1
            r["cost"] += float(j.estimated_cost_usd or 0)
            r["latency_total"] += int(j.latency_ms or 0)

        providers = []
        for row in provider_map.values():
            row["share"] = (row["count"] / total_completed * 100) if total_completed else 0
            row["cost_share"] = (row["cost"] / total_cost * 100) if total_cost else 0
            row["avg_latency"] = int(row["latency_total"] / row["count"]) if row["count"] else 0
            row["avg_cost"] = row["cost"] / row["count"] if row["count"] else 0
            row["models_text"] = ", ".join(
                f"{name} × {count}" for name, count in sorted(row["models"].items(), key=lambda x: -x[1])
            )
            providers.append(row)
        providers.sort(key=lambda x: (-x["count"], x["provider"]))

        tasks = []
        for row in task_map.values():
            row["share"] = (row["count"] / total_completed * 100) if total_completed else 0
            row["avg_latency"] = int(row["latency_total"] / row["count"]) if row["count"] else 0
            tasks.append(row)
        tasks.sort(key=lambda x: (-x["count"], x["task"]))

        routes = []
        for row in route_map.values():
            row["share"] = (row["count"] / total_completed * 100) if total_completed else 0
            row["avg_latency"] = int(row["latency_total"] / row["count"]) if row["count"] else 0
            routes.append(row)
        routes.sort(key=lambda x: (-x["count"], x["route"]))

        sources = [
            {"source": source, "count": count, "share": (count / total_completed * 100) if total_completed else 0}
            for source, count in sorted(source_map.items(), key=lambda x: (-x[1], x[0]))
        ]

        provider_names = [p["provider"] for p in providers]
        # Include connected providers from route configuration even when there is no traffic yet.
        for r in db.scalars(select(ModelRoute).where(ModelRoute.enabled.is_(True))).all():
            if r.provider not in provider_names:
                provider_names.append(r.provider)

        routing_preview = []
        preview_prompt = "Orientační routing preview pro Fresh AI Engine."
        for task_type in available_text_types:
            cells = {}
            for quality in QUALITIES:
                candidates = route_candidates(
                    db, task_type, quality, preview_prompt, 600, None
                )
                if candidates:
                    c = candidates[0]
                    cells[quality] = {
                        "route": c.route.code,
                        "provider": c.route.provider,
                        "model": c.route.model_name,
                        "score": round(c.score, 1),
                        "estimated_cost": c.estimated_cost_usd,
                    }
                else:
                    cells[quality] = None
            routing_preview.append({"task": task_type, "cells": cells})

        attempt_stmt = select(JobAttempt)
        if since is not None:
            attempt_stmt = attempt_stmt.where(JobAttempt.created_at >= since)
        attempts = db.scalars(attempt_stmt).all()
        fallback_count = 0
        failed_attempts = 0
        jobs_attempt_counts = {}
        for a in attempts:
            jobs_attempt_counts[a.job_id] = jobs_attempt_counts.get(a.job_id, 0) + 1
            if a.status == "failed":
                failed_attempts += 1
        fallback_count = sum(1 for count in jobs_attempt_counts.values() if count > 1)

        return render_template(
            "routing_analytics.html",
            period=period,
            total_completed=total_completed,
            total_cost=total_cost,
            providers=providers,
            tasks=tasks,
            routes=routes,
            sources=sources,
            matrix=matrix,
            provider_names=provider_names,
            routing_preview=routing_preview,
            fallback_count=fallback_count,
            failed_attempts=failed_attempts,
        )


@app.get("/admin/models")
@require_admin
def admin_models():
    providers = build_providers()
    with SessionLocal() as db:
        rows = db.scalars(select(ModelRoute).order_by(ModelRoute.provider, ModelRoute.priority.desc())).all()
        availability = {name: provider.available() for name, provider in providers.items()}
        profiles = {row.id: profile_json_for_admin(row) for row in rows}
        return render_template("models.html", models=rows, availability=availability, routing_profiles=profiles)


@app.post("/admin/models/<int:model_id>/update")
@require_admin
def admin_update_model(model_id: int):
    with SessionLocal() as db:
        row = db.get(ModelRoute, model_id)
        if row:
            row.enabled = request.form.get("enabled") == "on"
            row.quality = max(1, min(5, int(request.form.get("quality", row.quality))))
            row.priority = int(request.form.get("priority", row.priority))
            row.capabilities = request.form.get("capabilities", row.capabilities)
            row.input_usd_per_million = max(0.0, float(request.form.get("input_price", row.input_usd_per_million)))
            row.output_usd_per_million = max(0.0, float(request.form.get("output_price", row.output_usd_per_million)))
            profile_text = (request.form.get("routing_profile_json") or "").strip()
            if profile_text:
                try:
                    parsed_profile = json.loads(profile_text)
                    normalized_profile = normalize_route_profile(parsed_profile)
                    if not normalized_profile:
                        raise ValueError("profil neobsahuje známé dovednosti")
                    row.routing_profile_json = json.dumps(normalized_profile, ensure_ascii=False, separators=(",", ":"))
                except Exception as exc:
                    flash("Routing profil nebyl uložen: " + str(exc), "error")
                    return redirect("/admin/models", code=303)
            else:
                row.routing_profile_json = None
            db.commit()
    return redirect("/admin/models", code=303)


TASK_TYPES = ["general", "chat", "analysis", "marketing", "seo", "coding"]
QUALITIES = ["auto", "economy", "balanced", "best"]


def _period_since(period: str):
    now = datetime.now(timezone.utc)
    mapping = {
        "24h": now - timedelta(hours=24),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "all": None,
    }
    return mapping.get(period, mapping["7d"])


def _apply_since(stmt, since):
    return stmt.where(Job.created_at >= since) if since is not None else stmt


def _test_form_context(created=None, values=None):
    providers = build_providers()
    with SessionLocal() as db:
        routes = db.scalars(
            select(ModelRoute).where(ModelRoute.enabled.is_(True)).order_by(
                ModelRoute.priority.desc(), ModelRoute.code
            )
        ).all()
        task_types = [c.code for c in definitions(db) if c.code not in MEDIA_CAPABILITIES]
    availability = {name: provider.available() for name, provider in providers.items()}
    form_values = values or {
        "task_type": "",
        "operation": "",
        "quality": "auto",
        "force_route": "",
        "prompt": "Krátce potvrď, že Fresh AI Engine funguje.",
    }
    return dict(
        created=created,
        routes=routes,
        availability=availability,
        task_types=task_types,
        qualities=QUALITIES,
        form_values=form_values,
    )


@app.get("/admin/test")
@require_admin
def admin_test():
    return render_template("test.html", **_test_form_context())


@app.post("/admin/test")
@require_admin
def admin_test_submit():
    values = {
        "task_type": request.form.get("task_type", ""),
        "operation": request.form.get("operation", "").strip(),
        "quality": request.form.get("quality", "auto"),
        "force_route": request.form.get("force_route", "").strip(),
        "prompt": request.form.get("prompt", ""),
    }
    req = EngineRequest(
        source="admin-console",
        task_type=values["task_type"] or None,
        operation=values["operation"] or None,
        quality=values["quality"],
        prompt=values["prompt"],
    )
    with SessionLocal() as db:
        job = create_job(db, req)
        job = execute_job(db, job, force_route_code=values["force_route"] or None)
    return render_template("test.html", **_test_form_context(created=job, values=values))


@app.post("/admin/queue/toggle")
@require_admin
def admin_queue_toggle():
    with SessionLocal() as db:
        setting = db.get(RuntimeSetting, "queue_paused")
        if not setting:
            setting = RuntimeSetting(key="queue_paused", value="true")
            db.add(setting)
        else:
            setting.value = "false" if setting.value == "true" else "true"
        db.commit()
    return redirect("/admin", code=303)




def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        account_id=session.get('customer_account_id')
        if not account_id: return redirect('/account/login?next='+request.path)
        with SessionLocal() as db:
            account=db.get(CustomerAccount,account_id)
            if not account or not account.active or admin_suspended(db,account_id):
                session.clear(); return redirect('/account/login')
        return view(*args, **kwargs)
    return wrapped

# Fresh Key is registered only after both API and customer guards exist.
from app.fresh_key import register_fresh_key
register_fresh_key(app, require_fresh_key_api_key, customer_required)

class FreshAccountLinkRequired(ValueError):
    """Do not link a pre-existing local account solely because its email matches WordPress."""

class FreshAccountCreateRequired(ValueError):
    """Fresh Crackers identity is valid, but the user has not yet approved creating an AI Engine account."""


def _link_fresh_identity(db, remote, local_password=None, allow_create=False):
    external_id=str(remote.get('user_id') or remote.get('id') or '').strip()
    email=str(remote.get('email') or '').strip().lower()
    if not external_id or not email or not remote.get('verified', False):
        raise ValueError('Identita Fresh Crackers není úplná nebo e-mail ještě nebyl ověřen.')
    identity=db.scalar(select(ExternalIdentity).where(ExternalIdentity.provider=='freshcrackers',ExternalIdentity.external_user_id==external_id))
    if identity:
        account=db.get(CustomerAccount,identity.account_id)
        if not account or not account.active:
            raise ValueError('Propojený účet není aktivní. Kontaktuj podporu.')
        other=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email))
        if other and other.id!=account.id:
            raise FreshAccountLinkRequired('Tento e-mail již používá jiný účet AI Engine. Kontaktuj podporu pro bezpečné propojení.')
        if account.email!=email: account.email=email
        billing=remote.get('billing') or {}
        account.company_name=str(billing.get('company') or account.company_name or '')[:190]
        account.billing_name=str(billing.get('name') or account.billing_name or '')[:190]
        account.billing_ico=str(billing.get('ico') or account.billing_ico or '')[:40]
        account.billing_dic=str(billing.get('dic') or account.billing_dic or '')[:40]
        if billing:
            account.billing_address='\n'.join([x for x in [str(billing.get('address_1') or ''),str(billing.get('address_2') or ''),(' '.join([str(billing.get('postcode') or ''),str(billing.get('city') or '')])).strip(),str(billing.get('country') or '')] if x])[:2000]
        db.commit(); return account
    account=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email))
    if account is None:
        if not allow_create:
            raise FreshAccountCreateRequired('Účet Fresh Crackers je ověřený, ale Fresh AI Engine účet ještě neexistuje.')
        billing=remote.get('billing') or {}
        account=CustomerAccount(
            email=email,
            password_hash='external:'+secrets.token_urlsafe(40),
            company_name=str(billing.get('company') or '')[:190],
            billing_name=str(billing.get('name') or '')[:190],
            billing_ico=str(billing.get('ico') or '')[:40],
            billing_dic=str(billing.get('dic') or '')[:40],
            billing_address='\n'.join([x for x in [str(billing.get('address_1') or ''),str(billing.get('address_2') or ''),(' '.join([str(billing.get('postcode') or ''),str(billing.get('city') or '')])).strip(),str(billing.get('country') or '')] if x])[:2000]
        )
        db.add(account); db.flush()
    else:
        previous_identity=db.get(ExternalIdentity,account.id)
        if previous_identity:
            raise FreshAccountLinkRequired('Účet je již propojen s jinou identitou. Kontaktuj podporu.')
        # SSO proves ownership of the central Fresh Account. A shell created by
        # a trusted Fresh app through Fresh Key has no local password by design
        # and may therefore be linked directly on the first central login. Older
        # independently registered local accounts still require their password.
        bootstrap_shell = str(account.password_hash or '').startswith('external:fresh-key-bootstrap:')
        if not bootstrap_shell and not (local_password and not account.password_hash.startswith('external:')
                and check_password_hash(account.password_hash,local_password)):
            raise FreshAccountLinkRequired('Potvrď heslo původního účtu Fresh AI Engine, abychom účty bezpečně propojili.')
        if not account.active:
            raise ValueError('Původní účet AI Engine již není aktivní.')
    db.add(ExternalIdentity(account_id=account.id,provider='freshcrackers',external_user_id=external_id))
    row=db.get(AccountEmailVerification,account.id)
    now=datetime.now(timezone.utc)
    if row is None:
        db.add(AccountEmailVerification(account_id=account.id,token_hash='fresh-'+secrets.token_hex(24),expires_at=now,sent_at=now,verified_at=now))
    elif row.verified_at is None:
        row.verified_at=now
    db.commit(); return account


def _pending_remote(remote):
    return {
        'user_id':str(remote.get('user_id') or ''),
        'email':str(remote.get('email') or ''),
        'display_name':str(remote.get('display_name') or ''),
        'billing':remote.get('billing') or {},
        'verified':bool(remote.get('verified')),
        'since':int(datetime.now(timezone.utc).timestamp())
    }


def _safe_customer_next(value):
    value=str(value or '').strip()
    if value.startswith('/') and not value.startswith('//'):
        return value
    return ''


def _customer_login_redirect(default='/account'):
    target=_safe_customer_next(session.pop('customer_login_next',None)) or default
    return redirect(target,code=303)


def _complete_fresh_login(remote, local_password=None, allow_create=False):
    try:
        with SessionLocal() as db:
            account=_link_fresh_identity(db,remote,local_password=local_password,allow_create=allow_create)
        session.pop('fresh_link_pending',None)
        session.pop('fresh_create_pending',None)
        session['customer_account_id']=account.id
        return _customer_login_redirect()
    except FreshAccountCreateRequired:
        session['fresh_create_pending']=_pending_remote(remote)
        return render_template('account_create_from_fresh.html',remote=remote,error=None),200
    except FreshAccountLinkRequired as exc:
        session['fresh_link_pending']=_pending_remote(remote)
        return render_template('account_link.html',email=remote['email'],error=None,notice=str(exc)),409


@app.route('/account/create-from-fresh',methods=['GET','POST'])
def customer_create_from_fresh():
    pending=session.get('fresh_create_pending')
    now=int(datetime.now(timezone.utc).timestamp())
    if not isinstance(pending,dict) or now-int(pending.get('since',0))>600 or not pending.get('verified'):
        session.pop('fresh_create_pending',None)
        return redirect('/account/login')
    if request.method=='POST':
        if request.form.get('confirm')!='1':
            return render_template('account_create_from_fresh.html',remote=pending,error='Pro pokračování potvrď vytvoření účtu Fresh AI Engine.'),400
        return _complete_fresh_login(pending,allow_create=True)
    return render_template('account_create_from_fresh.html',remote=pending,error=None)


@app.route('/account/link',methods=['GET','POST'])
def customer_link_fresh():
    pending=session.get('fresh_link_pending')
    now=int(datetime.now(timezone.utc).timestamp())
    if not isinstance(pending,dict) or now-int(pending.get('since',0))>600:
        session.pop('fresh_link_pending',None)
        return redirect('/account/login')
    if request.method=='POST':
        password=request.form.get('password','')
        try:
            with SessionLocal() as db:
                account=_link_fresh_identity(db,pending,local_password=password)
            session.pop('fresh_link_pending',None)
            session['customer_account_id']=account.id
            return _customer_login_redirect()
        except (FreshAccountLinkRequired,ValueError):
            return render_template('account_link.html',email=pending['email'],error='Heslo původního účtu AI Engine není správné nebo účty nelze propojit.',notice=None),400
    return render_template('account_link.html',email=pending['email'],error=None,notice=None)

def _fresh_identity(db, account_id):
    return db.get(ExternalIdentity,account_id)

@app.route('/account/login', methods=['GET','POST'])
def customer_login():
    raw_next = request.form.get('next') if request.method == 'POST' else request.args.get('next')
    next_target = _safe_customer_next(raw_next)
    if next_target: session['customer_login_next']=next_target
    if session.get('customer_account_id'): return _customer_login_redirect()
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        if fresh_account.configured():
            try:
                remote=fresh_account.password_login(email,password)
                return _complete_fresh_login(remote,local_password=password)
            except fresh_account.FreshAccountError as exc:
                if exc.code in {'unverified','email_unverified'}:
                    return render_template('account_login.html',error='Účet Fresh Crackers ještě není ověřený.',unverified_email=email,fresh_enabled=True),403
                if exc.status not in {401,404}:
                    app.logger.warning('Fresh Account login failed: %s',exc.code)
                    return render_template('account_login.html',error='Přihlášení přes Fresh Crackers je nyní nedostupné. Zkus to za chvíli.',unverified_email=None,fresh_enabled=True),503
        with SessionLocal() as db:
            row=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email, CustomerAccount.active.is_(True)))
            # Backward-compatible local login: a pre-existing local account may also
            # be linked to Fresh Crackers. A valid local password still proves account
            # ownership and must remain usable during the Fresh Key migration. Accounts
            # created as SSO-only use an `external:` sentinel and cannot authenticate
            # through this fallback.
            account=None
            if row and not str(row.password_hash or '').startswith('external:'):
                account=authenticate(db,email,password)
            if account:
                if not is_verified(db,account.id):
                    return render_template('account_login.html',error='Účet ještě není ověřený.',unverified_email=email,fresh_enabled=fresh_account.configured()),403
                session['customer_account_id']=account.id; return _customer_login_redirect()
            if row and not is_verified(db,row.id):
                return render_template('account_login.html',error='Účet ještě není ověřený.',unverified_email=email,fresh_enabled=fresh_account.configured()),403
        return render_template('account_login.html',error='Neplatný e-mail nebo heslo.',unverified_email=None,fresh_enabled=fresh_account.configured()),401
    return render_template('account_login.html',error=None,unverified_email=None,fresh_enabled=fresh_account.configured())

@app.get('/account/login/fresh')
def customer_login_fresh():
    if not fresh_account.configured(): return redirect('/account/login')
    state=secrets.token_urlsafe(24); session['fresh_sso_state']=state
    callback=request.url_root.rstrip('/')+'/account/login/fresh/callback'
    return redirect(fresh_account.sso_start_url(callback,state))

@app.get('/account/login/fresh/callback')
def customer_login_fresh_callback():
    state=request.args.get('state',''); code=request.args.get('code','')
    expected=session.pop('fresh_sso_state',None)
    if not expected or not secrets.compare_digest(expected,state) or not code:
        return render_template('account_login.html',error='Přihlášení přes Fresh Crackers nebylo možné ověřit.',unverified_email=None,fresh_enabled=fresh_account.configured()),400
    try:
        remote=fresh_account.exchange_code(code)
        return _complete_fresh_login(remote)
    except Exception as exc:
        app.logger.warning('Fresh SSO callback failed: %s',type(exc).__name__)
        return render_template('account_login.html',error='Přihlášení přes Fresh Crackers se nezdařilo. Zkus to znovu.',unverified_email=None,fresh_enabled=fresh_account.configured()),400

@app.route('/account/register', methods=['GET','POST'])
def customer_register():
    if session.get('customer_account_id'): return redirect('/account')
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password',''); confirm=request.form.get('password_confirm','')
        privacy=request.form.get('privacy_ack')=='1'
        if not email or '@' not in email: return render_template('account_register.html',error='Zadej platný e-mail.',fresh_enabled=fresh_account.configured()),400
        if len(password)<10: return render_template('account_register.html',error='Heslo musí mít alespoň 10 znaků.',fresh_enabled=fresh_account.configured()),400
        if password!=confirm: return render_template('account_register.html',error='Hesla se neshodují.',fresh_enabled=fresh_account.configured()),400
        if not privacy: return render_template('account_register.html',error='Pro vytvoření účtu potvrď seznámení se zásadami ochrany osobních údajů.',fresh_enabled=fresh_account.configured()),400
        if fresh_account.configured():
            try:
                result=fresh_account.register(email,password,True,request.url_root.rstrip('/')+'/account/login')
                if result.get('existing_account') and result.get('verified') and result.get('user'):
                    # Existing WooCommerce account: validate the supplied password
                    # on WordPress before linking; no second registration or email.
                    session['fresh_existing_notice']=True
                    return _complete_fresh_login(result['user'],local_password=password)
                return render_template('account_register_done.html',email=email,send_error=None,central=True)
            except fresh_account.FreshAccountError as exc:
                if exc.code=='account_exists':
                    return render_template('account_register.html',error='Na Fresh Crackers již existuje účet s tímto e-mailem. Přihlas se účtem Fresh Crackers nebo obnov své heslo.',fresh_enabled=True,existing_account=True),409
                return render_template('account_register.html',error=str(exc),fresh_enabled=True),exc.status if 400<=exc.status<500 else 503
        try:
            with SessionLocal() as db:
                account=create_account(db,email,password); token=verification_token(db,account.id); verify_url=request.url_root.rstrip('/')+'/account/verify?token='+token
                try:
                    send_email(db,email,'Ověření účtu Fresh AI Engine','Potvrď svůj účet: '+verify_url,'<h2>Ověření účtu</h2><p><a href="'+verify_url+'">Potvrdit e-mail</a></p>')
                except Exception as exc:
                    app.logger.error('Verification email failed: %s',exc)
                    return render_template('account_register_done.html',email=email,send_error='Účet byl vytvořen, ale ověřovací e-mail se nepodařilo odeslat.',central=False),503
            return render_template('account_register_done.html',email=email,central=False)
        except ValueError as exc: return render_template('account_register.html',error=str(exc),fresh_enabled=False),400
    return render_template('account_register.html',error=None,fresh_enabled=fresh_account.configured())

@app.get('/account/verify')
def customer_verify():
    token=(request.args.get('token') or '').strip()
    with SessionLocal() as db: account=verify_email_token(db,token) if token else None
    if not account: return render_template('account_verify.html',ok=False),400
    return render_template('account_verify.html',ok=True)

@app.get('/account/verification')
def customer_verification_page(): return render_template('account_verification_resend.html',notice=None)

@app.post('/account/verification/resend')
def customer_verification_resend():
    email=request.form.get('email','').strip().lower(); notice='Pokud účet existuje a ještě není ověřený, poslali jsme nový ověřovací odkaz.'
    if fresh_account.configured():
        try: fresh_account.resend_verification(email,request.url_root.rstrip('/')+'/account/login')
        except Exception as exc: app.logger.warning('Central verification resend failed: %s',exc)
        return render_template('account_verification_resend.html',notice=notice)
    with SessionLocal() as db:
        account=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email))
        if account and not is_verified(db,account.id):
            token=verification_token(db,account.id); verify_url=request.url_root.rstrip('/')+'/account/verify?token='+token
            try: send_email(db,email,'Ověření účtu Fresh AI Engine','Potvrď svůj účet: '+verify_url,'<p><a href="'+verify_url+'">Potvrdit e-mail</a></p>')
            except Exception as exc: app.logger.error('Verification resend failed: %s',exc)
    return render_template('account_verification_resend.html',notice=notice)

@app.route('/account/password/forgot', methods=['GET','POST'])
def customer_password_forgot():
    notice=None
    if request.method=='POST':
        email=request.form.get('email','').strip().lower()
        if fresh_account.configured():
            try: fresh_account.password_forgot(email)
            except Exception as exc: app.logger.warning('Central password reset failed: %s',exc)
        with SessionLocal() as db:
            account=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email,CustomerAccount.active.is_(True)))
            identity=_fresh_identity(db,account.id) if account else None
            if account and not identity:
                token=password_reset_token(db,account.id,60); reset_url=request.url_root.rstrip('/')+'/account/password/reset?token='+token
                try: send_email(db,email,'Obnova hesla Fresh AI Engine','Nastav nové heslo: '+reset_url,'<p><a href="'+reset_url+'">Nastavit nové heslo</a></p>')
                except Exception as exc: app.logger.error('Password reset email failed: %s',exc)
        notice='Pokud účet s tímto e-mailem existuje, poslali jsme pokyny k obnově hesla.'
    return render_template('account_password_forgot.html',notice=notice)

@app.route('/account/password/reset', methods=['GET','POST'])
def customer_password_reset():
    token=(request.args.get('token') or request.form.get('token') or '').strip()
    if request.method=='POST':
        password=request.form.get('password',''); confirm=request.form.get('password_confirm','')
        if len(password)<10: return render_template('account_password_reset.html',token=token,error='Heslo musí mít alespoň 10 znaků.',ok=False),400
        if password!=confirm: return render_template('account_password_reset.html',token=token,error='Hesla se neshodují.',ok=False),400
        with SessionLocal() as db: account=reset_password(db,token,password) if token else None
        if not account: return render_template('account_password_reset.html',token='',error='Odkaz je neplatný, použitý nebo vypršel.',ok=False),400
        return render_template('account_password_reset.html',token='',error=None,ok=True)
    return render_template('account_password_reset.html',token=token,error=None,ok=False)

@app.post('/account/logout')
@customer_required
def customer_logout(): session.clear(); return redirect('/')

def customer_context(db, account_id):
    account=db.get(CustomerAccount,account_id); identity=_fresh_identity(db,account_id)
    connectors=db.scalars(select(Connector).where(Connector.account_id==account_id, Connector.deleted_at.is_(None)).order_by(Connector.created_at.desc())).all()
    ledger=customer_credit_activity(db,account_id,8)
    local_orders=db.scalars(select(CreditOrder).where(CreditOrder.account_id==account_id).order_by(CreditOrder.created_at.desc()).limit(20)).all()
    remote_orders=[]; remote_profile=None
    if identity and fresh_account.configured():
        try: remote_orders=fresh_account.orders(identity.external_user_id).get('orders',[])
        except Exception as exc: app.logger.warning('Order sync failed: %s',exc)
        try:
            remote_profile=fresh_account.profile(identity.external_user_id)
            billing=(remote_profile or {}).get('billing') or {}
            account.company_name=str(billing.get('company') or '')[:190]
            account.billing_name=str(billing.get('name') or '')[:190]
            account.billing_ico=str(billing.get('ico') or '')[:40]
            account.billing_dic=str(billing.get('dic') or '')[:40]
            account.billing_address='\n'.join([x for x in [str(billing.get('address_1') or ''),str(billing.get('address_2') or ''),(' '.join([str(billing.get('postcode') or ''),str(billing.get('city') or '')])).strip(),str(billing.get('country') or '')] if x])[:2000]
            db.commit()
        except Exception as exc: app.logger.warning('Fresh Account profile sync failed: %s',exc)
    return dict(existing_notice=bool(session.pop('fresh_existing_notice',False)),account=account,balance=balance(db,account_id),connectors=connectors,ledger=ledger,orders=local_orders,remote_orders=remote_orders,remote_profile=remote_profile,credit_price=credit_price_czk(db),fresh_identity=identity,fresh_enabled=fresh_account.configured())

@app.get('/account')
@customer_required
def customer_account():
    with SessionLocal() as db: return render_template('account.html',**customer_context(db,session['customer_account_id']))

@app.post('/account/profile')
@customer_required
def customer_profile_save():
    # Centrální účty spravují fakturační údaje na FreshCrackers.cz.
    with SessionLocal() as db:
        identity=_fresh_identity(db,session['customer_account_id'])
        if identity and fresh_account.configured():
            return redirect('https://www.freshcrackers.cz/muj-ucet/edit-address/',code=303)
        row=db.get(CustomerAccount,session['customer_account_id']); row.company_name=request.form.get('company_name','')[:190]; row.billing_name=request.form.get('billing_name','')[:190]
        row.billing_ico=request.form.get('billing_ico','')[:40]; row.billing_dic=request.form.get('billing_dic','')[:40]; row.billing_address=request.form.get('billing_address','')[:2000]; db.commit()
    return redirect('/account')

@app.post('/account/credits/order')
@customer_required
def customer_credit_order():
    try: credits=int(request.form.get('credits','0'))
    except (TypeError,ValueError): credits=0
    if credits < 1000:
        return redirect('/account?credit_error=minimum')
    with SessionLocal() as db:
        identity=_fresh_identity(db,session['customer_account_id'])
        if identity and fresh_account.configured():
            try:
                out=fresh_account.checkout(identity.external_user_id,credits,'default')
                if out.get('checkout_url'): return redirect(out['checkout_url'])
            except Exception as exc:
                app.logger.error('Fresh checkout failed: %s',exc)
                return redirect('/account?credit_error=checkout')
        return redirect('/account?credit_error=connect',code=303)

@app.post('/account/delete')
@customer_required
def customer_delete():
    if request.form.get('confirm','').strip().upper()!='SMAZAT': return redirect('/account?delete_error=1')
    aid=session['customer_account_id']
    with SessionLocal() as db:
        account=db.get(CustomerAccount,aid)
        if not account: session.clear(); return redirect('/')
        trash_account(db,account)
    session.clear(); return redirect('/account/goodbye', code=303)


@app.get('/account/goodbye')
def customer_goodbye():
    # Public landing page only; no deleted account information is exposed.
    return render_template('account_goodbye.html')

@app.get('/account/developer-api')
@customer_required
def customer_developer_api_page():
    """Shared, read-only developer reference for authenticated customers."""
    from app.developer_api import public_capability_catalog
    with SessionLocal() as db:
        return render_template('account_developer_api.html',
                               **customer_context(db, session['customer_account_id']),
                               capabilities=public_capability_catalog(db),
                               api_spec_url='/account/developer-api/openapi.json')

@app.get('/account/developer-api/openapi.json')
@customer_required
def customer_developer_api_openapi():
    from app.developer_api import build_openapi
    return jsonify(build_openapi())

@app.get('/account/connectors')
@customer_required
def customer_connectors():
    with SessionLocal() as db:
        return render_template('account_connectors.html',**customer_context(db,session['customer_account_id']),new_key=None,
            error=request.args.get('error',''),notice=request.args.get('notice',''))

@app.post('/account/connectors')
@customer_required
def customer_connector_create():
    name=request.form.get('name','').strip()[:120]
    if not name: return redirect('/account/connectors?error=name')
    try: expires_at=connector_expiry_from_form(request.form)
    except ValueError: return redirect('/account/connectors?error=validity')
    with SessionLocal() as db:
        row,key=create_connector(db,session['customer_account_id'],name,expires_at=expires_at)
        return render_template('account_connectors.html',**customer_context(db,session['customer_account_id']),new_key=key,new_connector=row,error='')

@app.post('/account/connectors/<connector_id>/update')
@customer_required
def customer_connector_update(connector_id):
    try: expires_at=connector_expiry_from_form(request.form)
    except ValueError: return redirect('/account/connectors?error=validity')
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=session['customer_account_id'] or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        name=request.form.get('name','').strip()[:120]
        if not name: return redirect('/account/connectors?error=name')
        row.name=name; row.expires_at=expires_at; row.active=request.form.get('active')=='on'; db.commit()
    return redirect('/account/connectors?notice=saved')

@app.post('/account/connectors/<connector_id>/rotate')
@customer_required
def customer_connector_rotate(connector_id):
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=session['customer_account_id'] or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        key=rotate_connector(db,row)
        return render_template('account_connectors.html',**customer_context(db,session['customer_account_id']),new_key=key,new_connector=row,error='')

@app.post('/account/connectors/<connector_id>/toggle')
@customer_required
def customer_connector_toggle(connector_id):
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=session['customer_account_id'] or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        row.active=not row.active; db.commit()
    return redirect('/account/connectors')

@app.post('/account/connectors/<connector_id>/delete')
@customer_required
def customer_connector_delete(connector_id):
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=session['customer_account_id'] or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        delete_connector(db,row)
    return redirect('/account/connectors?notice=deleted')

@app.get('/account/models')
@customer_required
def customer_models():
    with SessionLocal() as db:
        rows=db.scalars(select(ModelRoute).where(ModelRoute.enabled.is_(True)).order_by(ModelRoute.provider,ModelRoute.priority.desc())).all()
        availability={k:v.available() for k,v in build_providers().items()}
        return render_template('account_models.html',account=db.get(CustomerAccount,session['customer_account_id']),models=rows,availability=availability)

@app.get('/account/costs')
@customer_required
def customer_costs():
    aid=session['customer_account_id']
    connector_filter=(request.args.get('connector') or '')[:36]
    with SessionLocal() as db:
        price=credit_price_czk(db)
        all_rows=db.execute(
            select(JobBilling,Job,Connector)
            .join(Job,Job.id==JobBilling.job_id)
            .join(Connector,Connector.id==JobBilling.connector_id)
            .where(JobBilling.account_id==aid, Job.status=='completed')
            .order_by(Job.created_at.desc())
        ).all()
        summary={}
        for bill,job,connector in all_rows:
            r=summary.setdefault(connector.id,{'id':connector.id,'name':connector.name,'jobs':0,'credits':0.0,
                'value_czk':0.0,'cost_usd':0.0,'input_tokens':0,'output_tokens':0})
            charged=max(0.0,float(bill.credits_charged or 0))
            unit=float(bill.credit_unit_price_czk) if bill.credit_unit_price_czk is not None else price
            r['jobs']+=1; r['credits']+=charged; r['value_czk']+=charged*unit
            r['cost_usd']+=float(bill.cost_usd or 0)
            r['input_tokens']+=int(job.input_tokens or 0); r['output_tokens']+=int(job.output_tokens or 0)
        rows=[r for r in all_rows if not connector_filter or r[2].id==connector_filter][:20]
        activity=customer_credit_activity(db,aid,30)
        spent_credits=sum(max(0.0,float(r[0].credits_charged or 0)) for r in all_rows)
        spent_czk=sum(max(0.0,float(r[0].credits_charged or 0)) *
            (float(r[0].credit_unit_price_czk) if r[0].credit_unit_price_czk is not None else price) for r in all_rows)
        return render_template('account_costs.html',account=db.get(CustomerAccount,aid),
            balance=balance(db,aid),balance_czk=balance(db,aid)*price,credit_price=price,
            rows=rows,summary=sorted(summary.values(),key=lambda x:(-x['credits'],x['name'])),
            connector_filter=connector_filter,activity=activity,spent_credits=spent_credits,spent_czk=spent_czk,
            total_jobs=len(all_rows))

@app.get('/admin/accounts')
@require_admin
def admin_accounts():
    with SessionLocal() as db:
        accounts=db.scalars(select(CustomerAccount).where(CustomerAccount.active.is_(True)).order_by(CustomerAccount.created_at.desc())).all()
        fx=refresh_cnb_fx(db)
        finance_rows=accounts_finance_overview(db,[a.id for a in accounts])
        data=[]
        for a in accounts:
            data.append({'account':a,'balance':balance(db,a.id),
                'connectors':db.scalar(select(func.count(Connector.id)).where(Connector.account_id==a.id, Connector.deleted_at.is_(None))) or 0,
                'verified':is_verified(db,a.id),'suspended':admin_suspended(db,a.id),
                'linked':db.get(ExternalIdentity,a.id) is not None, 'finance':finance_rows[a.id]})
        total=dict(jobs=sum(r['finance']['jobs'] for r in data), balance_czk=sum(r['finance']['balance_czk'] for r in data),
                   credits=sum(r['finance']['credits'] for r in data), nominal_czk=sum(r['finance']['nominal_czk'] for r in data),
                   cost_usd=sum(r['finance']['cost_usd'] for r in data), cost_czk=sum(r['finance']['cost_czk'] for r in data),
                   input_tokens=sum(r['finance']['input_tokens'] for r in data), output_tokens=sum(r['finance']['output_tokens'] for r in data))
        complete=all(not r['finance']['missing_rates'] for r in data)
        total['profit_czk'] = total['nominal_czk']-total['cost_czk'] if complete else None
        total['margin_pct'] = 100*total['profit_czk']/total['nominal_czk'] if total['profit_czk'] is not None and total['nominal_czk'] else None
        return render_template('admin_accounts.html',accounts=data,total=total,fx=fx,
            credits_per_usd=credits_per_usd(db), credit_price=credit_price_czk(db),
            reporting_fx_rate=credit_setting(db, 'usd_czk_reporting_rate', 0.0),
            notice=request.args.get('notice',''), error=request.args.get('error',''))


@app.post('/admin/accounts/create')
@require_admin
def admin_account_create():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    email=request.form.get('email','').strip().lower()
    if len(email)>190 or not email or '@' not in email or email.startswith('deleted-'):
        return redirect('/admin/accounts?error=email',code=303)
    with SessionLocal() as db:
        if db.scalar(select(CustomerAccount).where(CustomerAccount.email==email)):
            return redirect('/admin/accounts?error=exists',code=303)
        # Local account by invitation; central identity never linked on e-mail match alone.
        account=create_account(db,email,secrets.token_urlsafe(48))
        account.company_name=request.form.get('company_name','').strip()[:190]
        # Explicit pending row also avoids legacy auto-verification after a restart.
        verification_token(db,account.id)
        db.commit()
        audit(db,account.id,'created','Lokální účet vytvořen administrátorem')
        db.commit()
        return _send_admin_invite(db,account)


def _send_admin_invite(db,account):
    token=make_invitation(db,account.id)
    url=os.getenv('FRESH_PUBLIC_ORIGIN','https://aiengine.freshcrackers.cz').rstrip('/')+'/account/invite?token='+token
    try:
        send_email(db,account.email,'Aktivace účtu Fresh AI Engine',
            'Nastav si své heslo a aktivuj účet na tomto odkazu (platnost 24 hodin): '+url,
            '<p>Pro nastavení hesla a aktivaci účtu otevři odkaz (platnost 24 hodin):</p><p><a href="'+url+'">Aktivovat účet</a></p>')
    except Exception:
        app.logger.exception('Admin account invitation delivery failed')
        return redirect('/admin/accounts/'+account.id+'?error=invite',code=303)
    audit(db,account.id,'invitation_sent','Aktivační e-mail odeslán')
    db.commit()
    return redirect('/admin/accounts/'+account.id+'?notice=invite',code=303)


@app.post('/admin/accounts/<account_id>/invite')
@require_admin
def admin_resend_invite(account_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if not account or not account.active or db.get(ExternalIdentity,account_id): return json_error('Účet nelze pozvat.',400)
        return _send_admin_invite(db,account)


@app.route('/account/invite',methods=['GET','POST'])
def invited_account_set_password():
    token=(request.args.get('token') or request.form.get('token') or '').strip()
    error=None
    if request.method=='POST':
        password=request.form.get('password',''); again=request.form.get('password_confirm','')
        if len(password)<10: error='Heslo musí mít alespoň 10 znaků.'
        elif password!=again: error='Hesla se neshodují.'
        else:
            with SessionLocal() as db:
                account=accept_invitation(db,token,password)
            if account:
                return redirect('/account/login?invited=1',code=303)
            error='Odkaz je neplatný nebo vypršel.'
    return render_template('account_invite.html',token=token,error=error)


@app.get('/admin/accounts/trash')
@require_admin
def admin_account_trash():
    with SessionLocal() as db:
        rows=db.execute(select(AccountTrash,CustomerAccount)
            .join(CustomerAccount,CustomerAccount.id==AccountTrash.account_id)
            .where(AccountTrash.purged_at.is_(None))
            .order_by(AccountTrash.deleted_at.desc())).all()
        return render_template('admin_account_trash.html',rows=rows,notice=request.args.get('notice',''))


@app.post('/admin/accounts/trash/empty')
@require_admin
def admin_account_empty_trash():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    if request.form.get('confirm','').strip()!='VYSYPAT': return redirect('/admin/accounts/trash?notice=confirm',code=303)
    with SessionLocal() as db: count=purge_trash(db)
    return redirect('/admin/accounts/trash?notice=purged-'+str(count),code=303)


@app.get('/admin/accounts/orders')
@require_admin
def admin_accounts_orders():
    try: page=max(1,int(request.args.get('page','1')))
    except ValueError: page=1
    if not fresh_account.configured():
        return render_template('admin_account_orders.html',orders=[],page=page,has_more=False,
            error='Pro synchronizaci je nutné nastavit propojení s Fresh Crackers.',source=''),503
    try:
        snapshot=fresh_account.admin_orders(page)
        if not isinstance(snapshot,dict) or snapshot.get('source')!='woocommerce':
            raise ValueError('Neplatná odpověď e-shopu')
        return render_template('admin_account_orders.html',orders=snapshot.get('orders',[]),page=page,
            has_more=bool(snapshot.get('has_more')),error=None,source=snapshot.get('source'))
    except Exception:
        app.logger.exception('Admin orders sync failed')
        return render_template('admin_account_orders.html',orders=[],page=page,has_more=False,
            error='Objednávky se nyní nepodařilo načíst z e-shopu. Stav objednávek zde neměň.',source=''),503


@app.get('/admin/chat-usage')
@require_admin
def admin_chat_usage():
    with SessionLocal() as db:
        sync_state=sync_chat_users(db)
        rows=chat_user_summary(db)
        total=dict(users=len([r for r in rows if r['id']!='legacy']),
            jobs=sum(r['jobs'] for r in rows),
            credits=sum(r['credits'] for r in rows),
            nominal_czk=sum(r['nominal_czk'] for r in rows),
            cost_usd=sum(r['cost_usd'] for r in rows),
            cost_czk=sum(r['cost_czk'] for r in rows),
            unbilled=sum(r['unbilled'] for r in rows))
        return render_template('admin_chat_usage.html',rows=rows,total=total,fx=fx_status(db),sync_state=sync_state)


@app.get('/admin/chat-usage/<int:user_id>')
@require_admin
def admin_chat_usage_detail(user_id):
    key='chat:'+str(user_id)
    with SessionLocal() as db:
        rows=chat_user_summary(db,key)
        if not rows:
            # A local-only user can exist in the small directory without billed jobs.
            row=db.get(ChatUsageUser,key)
            if row is None: return json_error('Uživatel Chatu nenalezen.',404)
            return redirect('/admin/chat-usage', code=303)
        return render_template('admin_chat_usage_detail.html',row=rows[0],fx=fx_status(db))


@app.get('/admin/accounts/<account_id>')
@require_admin
def admin_account_detail(account_id):
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if not account: return json_error('Účet nenalezen.',404)
        identity=db.get(ExternalIdentity,account_id)
        connectors=db.scalars(select(Connector).where(Connector.account_id==account_id, Connector.deleted_at.is_(None)).order_by(Connector.created_at.desc())).all()
        deleted_connectors=db.scalars(select(Connector).where(Connector.account_id==account_id, Connector.deleted_at.is_not(None)).order_by(Connector.deleted_at.desc())).all()
        remote_profile=None
        if identity and fresh_account.configured():
            try: remote_profile=fresh_account.profile(identity.external_user_id)
            except Exception as exc: app.logger.warning('Admin Fresh Account profile sync failed: %s',exc)
        credits=db.scalars(select(CreditLedger).where(CreditLedger.account_id==account_id).order_by(CreditLedger.created_at.desc()).limit(40)).all()
        tasks=db.execute(select(Job,JobBilling).join(JobBilling,JobBilling.job_id==Job.id).where(JobBilling.account_id==account_id).order_by(Job.created_at.desc()).limit(40)).all()
        logs=db.scalars(select(AdminAccountAudit).where(AdminAccountAudit.account_id==account_id).order_by(AdminAccountAudit.created_at.desc()).limit(20)).all()
        refresh_cnb_fx(db)
        finance, financial_projects = account_finance(db,account_id)
        return render_template('admin_account_detail.html',account=account,identity=identity,finance=finance,
            verified=is_verified(db,account_id),suspended=admin_suspended(db,account_id),
            balance=balance(db,account_id),connectors=connectors,deleted_connectors=deleted_connectors,ledger=credits,
            tasks=tasks,projects=financial_projects,logs=logs,remote_profile=remote_profile,
            new_connector_key=session.pop('admin_new_connector_key',None),
            new_connector_id=session.pop('admin_new_connector_id',None),
            notice=request.args.get('notice',''),error=request.args.get('error',''))


@app.post('/admin/accounts/<account_id>/update')
@require_admin
def admin_account_update(account_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if not account or not account.active: return json_error('Účet není aktivní.',400)
        identity=db.get(ExternalIdentity,account_id)
        changed=[]
        local_email_changed=False

        if identity:
            # Linked accounts remain centrally consistent: admin edits are written to Fresh Crackers first,
            # then mirrored locally for reporting/search. Passwords are never synchronized.
            if request.form.get('profile_loaded')!='1':
                return redirect('/admin/accounts/'+account_id+'?error=fresh-profile',code=303)
            if not fresh_account.configured():
                return redirect('/admin/accounts/'+account_id+'?error=fresh-connect',code=303)
            email=request.form.get('email',account.email).strip().lower()[:190]
            if '@' not in email: return redirect('/admin/accounts/'+account_id+'?error=email',code=303)
            other=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email,CustomerAccount.id!=account_id))
            if other: return redirect('/admin/accounts/'+account_id+'?error=exists',code=303)
            payload={
                'email':email,
                'first_name':request.form.get('first_name','').strip()[:120],
                'last_name':request.form.get('last_name','').strip()[:120],
                'company':request.form.get('company','').strip()[:190],
                'ico':request.form.get('ico','').strip()[:40],
                'dic':request.form.get('dic','').strip()[:40],
                'address_1':request.form.get('address_1','').strip()[:190],
                'address_2':request.form.get('address_2','').strip()[:190],
                'city':request.form.get('city','').strip()[:120],
                'postcode':request.form.get('postcode','').strip()[:40],
                'state':request.form.get('state','').strip()[:80],
                'country':request.form.get('country','').strip()[:12],
                'phone':request.form.get('phone','').strip()[:80],
                'billing_email':email,
            }
            try:
                remote=fresh_account.update_profile(identity.external_user_id,payload)
            except Exception:
                app.logger.exception('Admin Fresh Account profile update failed')
                return redirect('/admin/accounts/'+account_id+'?error=fresh-update',code=303)
            billing=(remote or {}).get('billing') or {}
            account.email=str((remote or {}).get('email') or email).lower()[:190]
            account.company_name=str(billing.get('company') or '')[:190]
            account.billing_name=str(billing.get('name') or '')[:190]
            account.billing_ico=str(billing.get('ico') or '')[:40]
            account.billing_dic=str(billing.get('dic') or '')[:40]
            account.billing_address='\n'.join([x for x in [
                str(billing.get('address_1') or ''),str(billing.get('address_2') or ''),
                (' '.join([str(billing.get('postcode') or ''),str(billing.get('city') or '')])).strip(),
                str(billing.get('country') or '')] if x])[:2000]
            changed.append('fresh_profile')
        else:
            fields=['email','company_name','billing_name','billing_ico','billing_dic','billing_address']
            for field in fields:
                current=getattr(account,field)
                limit=2000 if field=='billing_address' else 190 if field in ('email','company_name','billing_name') else 40
                value=request.form.get(field,current).strip()[:limit]
                if field=='email':
                    value=value.lower()
                    if '@' not in value: return redirect('/admin/accounts/'+account_id+'?error=email',code=303)
                    other=db.scalar(select(CustomerAccount).where(CustomerAccount.email==value,CustomerAccount.id!=account_id))
                    if other: return redirect('/admin/accounts/'+account_id+'?error=exists',code=303)
                    if value!=current:
                        invitation=db.get(AccountInvitation,account_id)
                        if invitation: db.delete(invitation)
                        verification_token(db,account_id)
                        local_email_changed=True
                if value!=current:
                    setattr(account,field,value); changed.append(field)

        row=db.get(AccountAdminState,account_id)
        if row is None:
            row=AccountAdminState(account_id=account_id,suspended=False); db.add(row)
        new_suspended=request.form.get('suspended')=='on'
        if bool(row.suspended)!=new_suspended:
            row.suspended=new_suspended; changed.append('suspended')
        if changed: audit(db,account_id,'edited',','.join(changed))
        db.commit()
        if local_email_changed:
            return _send_admin_invite(db,account)
        return redirect('/admin/accounts/'+account_id+'?notice=saved',code=303)


@app.post('/admin/accounts/<account_id>/verify')
@require_admin
def admin_account_verify(account_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if account and account.active:
            manually_verify_account(db,account_id)
            audit(db,account_id,'verified','Ručně ověřeno'); db.commit()
    return redirect('/admin/accounts/'+account_id,code=303)


@app.post('/admin/accounts/<account_id>/credits')
@require_admin
def admin_account_adjust_credits(account_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    try: amount=round(float(request.form.get('amount','0')),4)
    except ValueError: amount=0
    reason=request.form.get('reason','').strip()[:120]
    if not reason or amount==0 or abs(amount)>1_000_000:
        return redirect('/admin/accounts/'+account_id+'?error=credit',code=303)
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if not account or not account.active: return json_error('Účet není aktivní.',400)
        if amount<0 and balance(db,account_id)+amount<0:
            return redirect('/admin/accounts/'+account_id+'?error=balance',code=303)
        db.add(CreditLedger(account_id=account_id,delta_credits=amount,entry_type='admin_adjustment',
            reference='admin-'+secrets.token_hex(12),note=reason))
        audit(db,account_id,'credit_adjustment',str(amount)+' kreditů; důvod: '+reason)
        db.commit()
    return redirect('/admin/accounts/'+account_id+'?notice=credits',code=303)


@app.post('/admin/accounts/<account_id>/connectors/create')
@require_admin
def admin_account_connector_create(account_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    name=request.form.get('name','').strip()[:120]
    if not name: return redirect('/admin/accounts/'+account_id+'?error=connector-name',code=303)
    try: expires_at=connector_expiry_from_form(request.form)
    except ValueError: return redirect('/admin/accounts/'+account_id+'?error=connector-validity',code=303)
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if not account or not account.active: return json_error('Účet není aktivní.',400)
        row,key=create_connector(db,account_id,name,expires_at=expires_at)
        audit(db,account_id,'connector_created',row.id); db.commit()
        session['admin_new_connector_key']=key; session['admin_new_connector_id']=row.id
    return redirect('/admin/accounts/'+account_id+'?notice=connector-created',code=303)


@app.post('/admin/accounts/<account_id>/connector/<connector_id>')
@require_admin
def admin_account_connector_update(account_id,connector_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    try: expires_at=connector_expiry_from_form(request.form)
    except ValueError: return redirect('/admin/accounts/'+account_id+'?error=connector-validity',code=303)
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=account_id or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        name=request.form.get('name',row.name).strip()[:120]
        if not name: return redirect('/admin/accounts/'+account_id+'?error=connector-name',code=303)
        row.name=name
        row.active=request.form.get('active')=='on'
        row.expires_at=expires_at
        audit(db,account_id,'connector_edited',connector_id)
        db.commit()
    return redirect('/admin/accounts/'+account_id+'?notice=connector',code=303)


@app.post('/admin/accounts/<account_id>/connector/<connector_id>/rotate')
@require_admin
def admin_account_connector_rotate(account_id,connector_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=account_id or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        key=rotate_connector(db,row)
        audit(db,account_id,'connector_rotated',connector_id); db.commit()
        session['admin_new_connector_key']=key; session['admin_new_connector_id']=row.id
    return redirect('/admin/accounts/'+account_id+'?notice=connector-key',code=303)


@app.post('/admin/accounts/<account_id>/connector/<connector_id>/delete')
@require_admin
def admin_account_connector_delete(account_id,connector_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        row=db.get(Connector,connector_id)
        if not row or row.account_id!=account_id or row.deleted_at is not None: return json_error('Konektor nenalezen.',404)
        delete_connector(db,row)
        audit(db,account_id,'connector_deleted',connector_id); db.commit()
    return redirect('/admin/accounts/'+account_id+'?notice=connector-deleted',code=303)


@app.post('/admin/accounts/<account_id>/delete')
@require_admin
def admin_account_delete(account_id):
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    if request.form.get('confirm','').strip().upper()!='SMAZAT':
        return redirect('/admin/accounts/'+account_id+'?error=delete-confirm',code=303)
    with SessionLocal() as db:
        account=db.get(CustomerAccount,account_id)
        if not account or not account.active: return json_error('Účet není aktivní.',400)
        trash_account(db,account)
    return redirect('/admin/accounts?notice=deleted',code=303)


@app.post('/admin/accounts/refresh-fx')
@require_admin
def admin_refresh_fx():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        status=refresh_cnb_fx(db, force=True)
    return redirect('/admin/accounts?notice=fx-ok' if status['state']=='ok' else '/admin/accounts?error=fx-unavailable',code=303)


@app.post('/admin/accounts/settings')
@require_admin
def admin_credit_settings():
    if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
    with SessionLocal() as db:
        value=request.form.get('credits_per_usd','1000')
        try: value_num=float(value)
        except ValueError: value_num=0
        if value_num<=0 or value_num>10_000_000: return redirect('/admin/accounts?error=settings',code=303)
        row=db.get(RuntimeSetting,'credits_per_usd')
        if row: row.value=str(value_num)
        else: db.add(RuntimeSetting(key='credits_per_usd',value=str(value_num)))
        # FX is refreshed from CNB (fixed official URL), not from user-supplied input.
        # Customer credit price is an indicative nominal value, owned by WooCommerce.
        db.commit()
    return redirect('/admin/accounts?notice=settings',code=303)


@app.post('/admin/accounts/orders/<order_id>/paid')
@require_admin
def admin_order_paid_disabled(order_id):
    return json_error('Objednávky a jejich stav se spravují výhradně v e-shopu Fresh Crackers.',410)


@app.route('/admin/smtp',methods=['GET','POST'])
@require_admin
def admin_smtp():
    notice=None; error=None
    with SessionLocal() as db:
        if request.method=='POST':
            if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
            action=request.form.get('action','save')
            try:
                for key in ['smtp_host','smtp_port','smtp_user','smtp_from_email','smtp_from_name','smtp_security']:
                    value=(request.form.get(key,'') or '').strip()
                    row=db.get(RuntimeSetting,key)
                    if row: row.value=value
                    else: db.add(RuntimeSetting(key=key,value=value))
                enabled='true' if request.form.get('smtp_enabled')=='on' else 'false'
                row=db.get(RuntimeSetting,'smtp_enabled')
                if row: row.value=enabled
                else: db.add(RuntimeSetting(key='smtp_enabled',value=enabled))
                password=request.form.get('smtp_password','')
                if password: save_env_secret('SMTP_PASSWORD',password)
                db.commit()
                if action=='test':
                    target=(request.form.get('test_email') or request.form.get('smtp_from_email') or '').strip()
                    if not target: raise ValueError('Zadej e-mail pro test.')
                    send_email(db,target,'Test SMTP · Fresh AI Engine','SMTP připojení Fresh AI Engine funguje.')
                    notice='Testovací e-mail byl odeslán.'
                else: notice='SMTP nastavení bylo uloženo.'
            except smtplib.SMTPAuthenticationError as exc:
                # Never echo the provider's reply or any submitted password.
                error=('SMTP server odmítl přihlášení (kód ' + str(exc.smtp_code) +
                       '). Ověř uživatelské jméno schránky (často celou e-mailovou adresu) '
                       'a její poštovní heslo. Nastavení bylo uloženo, test e-mailu neprošel.')
            except Exception as exc:
                error=f'{type(exc).__name__}: {str(exc)[:300]}'
        return render_template('smtp.html',cfg=smtp_config(db),notice=notice,error=error)

@app.route('/admin/database',methods=['GET','POST'])
@require_admin
def admin_database():
    result=None; error=None
    if request.method=='POST':
        if not admin_post_allowed(): return json_error('Nedůvěryhodný původ formuláře.',403)
        host=request.form.get('host','localhost'); port=request.form.get('port','3306'); name=request.form.get('name',''); user=request.form.get('user',''); password=request.form.get('password','')
        action=request.form.get('action','test')
        try:
            test_connection(host,port,name,user,password)
            if action=='migrate':
                counts=migrate_current(host,port,name,user,password); backup=save_env_database(host,port,name,user,password)
                result='Migrace dokončena. Importováno: '+', '.join(f'{k}: {v}' for k,v in counts.items())+'. Konfigurace byla uložena; nyní restartuj Passenger.'
            else: result='Připojení k databázi je v pořádku.'
        except Exception as exc: error=f'{type(exc).__name__}: {str(exc)[:500]}'
    return render_template('database.html',result=result,error=error)

@app.errorhandler(404)
def not_found(_):
    return json_error("Not found", 404)


@app.errorhandler(500)
def internal_error(_):
    return json_error("Internal server error", 500)
