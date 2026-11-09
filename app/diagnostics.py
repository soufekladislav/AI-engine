"""Privacy-conscious, authenticated application manifest and support bundle."""
import hashlib
import io
import json
import os
import platform
import re
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from sqlalchemy import select, func
from app.config import APP_ROOT, get_settings
from app.models import (Job, JobAttempt, ModelRoute, RuntimeSetting, CapabilityDefinition, FreshKeyAppBinding, FreshKeyIdentity, FreshKeyProject, FreshKeyLocalProject, FreshKeyProjectBinding, FreshKeyProjectConnection, FreshKeyOAuthCredential)
from app.providers import build_providers
from app.routing_knowledge import effective_model_profile
from app.multimodal import available_capabilities
from app.provider_guard import all_provider_guards
from app.admin_alerts import list_admin_alerts
from app.fresh_key import provider_status

VERSION = '0.8.0'
EXTS = {'.py', '.html', '.css', '.js', '.json', '.md', '.txt', '.ini'}
EXCLUDED = {'.env', '.db', '.sqlite', '.sqlite3', 'stderr.log', 'stdout.log', '.htaccess'}
MAX_FILE = 300_000


def _safe_json(raw, default=None):
    """Best-effort parser for legacy/truncated diagnostic JSON.

    Diagnostics must never make the application unavailable merely because an
    older DB row contains malformed JSON.
    """
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default

def inventory():
    files = []
    for p in APP_ROOT.rglob('*'):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(APP_ROOT)
        if any(x in {'__pycache__', '.git', 'venv', '.venv', 'node_modules', 'tmp'} for x in rel.parts):
            continue
        if p.name in EXCLUDED or p.name.startswith('.env') or p.suffix in {'.pyc', '.log', '.zip'}:
            continue
        if p.suffix not in EXTS or p.stat().st_size > MAX_FILE:
            continue
        files.append({'path': rel.as_posix(), 'bytes': p.stat().st_size,
                      'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
    return sorted(files, key=lambda x:x['path'])

def report(db, include_jobs=True):
    settings = get_settings()
    providers = build_providers()
    routes = db.scalars(select(ModelRoute).order_by(ModelRoute.priority.desc())).all()
    summary = [{'status': state, 'count': count} for state, count in db.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)).all()]
    generated_utc = datetime.now(timezone.utc)
    media_caps = available_capabilities()
    image_ops = ("image.generate", "image_generation.generate")
    image_success = int(db.scalar(select(func.count(Job.id)).where(
        Job.operation.in_(image_ops), Job.status == "completed", Job.result_type == "image", Job.result_validated.is_(True)
    )) or 0)
    image_failed = int(db.scalar(select(func.count(Job.id)).where(Job.operation.in_(image_ops), Job.status == "failed")) or 0)
    image_legacy_misroutes = int(db.scalar(select(func.count(Job.id)).where(
        Job.operation.in_(image_ops), Job.status == "completed", Job.result_type != "image"
    )) or 0)
    last_image = db.scalar(select(Job).where(Job.operation.in_(image_ops)).order_by(Job.created_at.desc()).limit(1))
    last_image_trace = _safe_json(last_image.routing_decision.trace_json, {}) if last_image and last_image.routing_decision else {}
    last_asset = (last_image_trace or {}).get("media_asset") or {}
    image_block = media_caps.setdefault("image_generation", {})
    image_block.update({
        "adapter_status": "ready" if image_block.get("enabled") else "not_configured",
        "success_calls": image_success,
        "failed_calls": image_failed,
        "legacy_text_misroutes": image_legacy_misroutes,
        "last_run": ({
            "id": last_image.id,
            "status": last_image.status,
            "source": last_image.source,
            "capability": last_image.task_type,
            "operation": last_image.operation,
            "provider": last_image.provider,
            "model": last_image.model_name,
            "result_type": last_image.result_type,
            "mime_type": last_asset.get("mime_type"),
            "width": last_asset.get("width"),
            "height": last_asset.get("height"),
            "size_bytes": last_asset.get("size_bytes"),
            "cost_usd": float(last_image.estimated_cost_usd or 0),
            "cost_source": last_asset.get("cost_source"),
            "latency_ms": last_image.latency_ms,
            "error_category": (last_image.error or "").split(":", 1)[0][:60] or None,
            "created_at": last_image.created_at.isoformat() if last_image.created_at else None,
        } if last_image else None),
    })
    guard_states = all_provider_guards(db, list(providers.keys()))
    data = {
        'schema_version': 2, 'engine_version': VERSION,
        'generated_at': generated_utc.isoformat(),
        'generated_at_prague': generated_utc.astimezone(ZoneInfo('Europe/Prague')).isoformat(),
        'timezone_display': 'Europe/Prague',
        'runtime': {'python': platform.python_version(), 'platform': platform.system(),
                    'database_driver': db.bind.dialect.name, 'store_payloads': settings.store_payloads,
                    'worker_batch_size': settings.worker_batch_size, 'job_timeout_seconds': settings.job_timeout_seconds,
                    'log_preview_chars': settings.log_preview_chars},
        'providers': {name: {'configured': bool(p.available()), 'guard': guard_states.get(name, {'state':'closed'})} for name,p in providers.items()},
        'fresh_key': {
            'enabled': True,
            'providers': provider_status(),
            'counts': {
                'app_bindings': int(db.scalar(select(func.count(FreshKeyAppBinding.id))) or 0),
                'identities': int(db.scalar(select(func.count(FreshKeyIdentity.id))) or 0),
                'links': int(db.scalar(select(func.count(FreshKeyProject.id)).where(FreshKeyProject.active.is_(True))) or 0),
                'projects': int(db.scalar(select(func.count(FreshKeyProject.id)).where(FreshKeyProject.active.is_(True))) or 0),  # compatibility
                'local_projects': int(db.scalar(select(func.count(FreshKeyLocalProject.id)).where(FreshKeyLocalProject.active.is_(True))) or 0),
                'published_local_bindings': int(db.scalar(select(func.count(FreshKeyProjectBinding.id))) or 0),
                'unlinked_local_projects': max(0,
                    int(db.scalar(select(func.count(FreshKeyLocalProject.id)).where(FreshKeyLocalProject.active.is_(True))) or 0)
                    - int(db.scalar(select(func.count(FreshKeyProjectBinding.id))) or 0)
                ),
                'connections': int(db.scalar(select(func.count(FreshKeyProjectConnection.id)).where(FreshKeyProjectConnection.status != 'deleted')) or 0),
                'oauth_credentials': int(db.scalar(select(func.count(FreshKeyOAuthCredential.id)).where(FreshKeyOAuthCredential.status == 'connected')) or 0),
            },
            'secrets_exported': False,
            'model': 'shared identity + project linking + memberships + project-bound external connections; employee work data stays in each app',
        },
        'routes': [{'code':r.code, 'provider':r.provider, 'model':r.model_name,
                    'enabled':r.enabled, 'quality':r.quality, 'priority':r.priority,
                    'capabilities':r.capabilities, 'input_usd_per_million':r.input_usd_per_million,
                    'output_usd_per_million':r.output_usd_per_million,
                    'routing_profile': effective_model_profile(r)} for r in routes],
        'capability_definitions': [{'code':c.code, 'label':c.label, 'description':c.description,
                                    'enabled': (
                                        bool(media_caps.get('image_generation',{}).get('openai')) if c.code == 'image_generation' else
                                        bool(media_caps.get('image_understanding',{}).get('openai')) if c.code == 'image_understanding' else
                                        c.enabled
                                    ), 'priority':c.priority,
                                    'match_phrases':_safe_json(c.phrases_json, [])}
                                   for c in db.scalars(select(CapabilityDefinition)).all()],
        'media_capabilities': media_caps,
        'router': {'version': '2.3.0', 'normal_single_shot': True, 'structured_recovery': True,
                   'provider_failover': True, 'max_provider_failover_calls': 4,
                   'max_invalid_content_attempts': 2, 'provider_guard': True, 'circuit_breaker': True,
                   'fail_safe_history': True, 'default_quality': 'auto',
                   'policy': 'text: one primary route with bounded failover; media: operation-first dispatch to capability-specific adapters only'},
        'runtime_settings': {r.key: r.value for r in db.scalars(select(RuntimeSetting)).all()
                             if r.key in {'queue_paused'}},
        'job_summary': summary,
        'admin_alerts': [{k:v for k,v in a.items() if k not in {'message'}} for a in list_admin_alerts(db)[:10]],
        'files': inventory(),
        'ui': {'templates': [f['path'] for f in inventory() if f['path'].startswith('app/templates/')],
               'stylesheets': [f['path'] for f in inventory() if f['path'].endswith('.css')]},
        'security': {'credentials_exported': False, 'request_bodies_exported': False,
                     'database_exported': False, 'raw_logs_exported': False},
    }
    if include_jobs:
        jobs = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)).all()
        data['recent_jobs'] = [dict(id=j.id, status=j.status, source=j.source, task_type=j.task_type, operation=j.operation,
                                    quality=j.quality, provider=j.provider, model=j.model_name,
                                    result_type=j.result_type, schema_id=j.result_schema_id, result_validated=bool(j.result_validated),
                                    route=j.selected_route_code, cost_usd=j.estimated_cost_usd,
                                    latency_ms=j.latency_ms, created_at=j.created_at.isoformat() if j.created_at else None,
                                    error_category= (j.error or '').split(':',1)[0][:60] or None,
                                    routing_decision=_safe_json(j.routing_decision.trace_json, None) if j.routing_decision else None)
                               for j in jobs]
    return data

def support_bundle(db, manifest=None):
    """Source, configuration metadata, sanitized trace; never secret files or raw production logs."""
    out = io.BytesIO()
    manifest = manifest if manifest is not None else report(db)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr('README.txt', 'Support bundle Fresh AI Engine. No API keys, .env, DB, user prompts, raw provider output or raw server logs. Review sources before sharing.\n')
        for f in manifest['files']:
            path = APP_ROOT / f['path']
            # Only ship application source, never arbitrary root text/config files.
            if f['path'].startswith('app/') or f['path'] in {'flask_app.py','cron_worker.py','passenger_wsgi.py','requirements.txt','README.md','readme.md','CHANGELOG.md'}:
                z.write(path, 'source/' + f['path'])
    out.seek(0)
    return out
