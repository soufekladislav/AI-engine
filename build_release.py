#!/usr/bin/env python3
"""Build a ZIP of COMPLETE modified AI Engine source files from an exact checkout.

No changes to GitHub or production. Refuses changed upstream files rather than
silently applying string replacements to a different release.

Usage:
  python build_release.py --repo /path/to/AI-engine
  python build_release.py --download
"""
from __future__ import annotations
import argparse
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BASE_REPO = 'https://github.com/soufekladislav/AI-engine'
BASE_COMMIT = '717d5a67b085b4574ce1cbfcc1118ae683aba8de'
EXPECTED_BLOBS = {
    'app/accounts.py': '95c720656f4aa6ede6ee0f5240d93654aacb41b4',
    'app/models.py': 'bc385e9eaed8c11ddb06bb35c0a7117a06df6daf',
    'app/engine.py': '46da8177c32b335c5d73961477bcccdcef4be41f',
    'flask_app.py': '287d9739e890e8c658d9a2e14f61f9b35c3bfa2a',
}
NEW_FILES = ('app/cost_models.py','app/cost_accounting.py')


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def expect_replace(src: str, before: str, after: str, name: str) -> str:
    count = src.count(before)
    if count != 1:
        raise RuntimeError(f'{name}: expected exactly one anchor, found {count}: {before[:100]!r}')
    return src.replace(before, after, 1)


def replace_function(src: str, name: str, new: str) -> str:
    pattern = re.compile(r'^def ' + re.escape(name) + r'\(.*?(?=^def |^class |^@app\.|\Z)', re.M | re.S)
    m = pattern.search(src)
    if not m:
        raise RuntimeError(f'Function not found: {name}')
    return src[:m.start()] + new.rstrip() + '\n\n' + src[m.end():]


def edit_models(s: str) -> str:
    return s.rstrip() + ('\n\n# Fresh AI Engine 0.8.1: auditable cost models, registered for create_all().\n'
        'from app.cost_models import ProviderTariffVersion, AttemptCostRecord, JobBillingTerms, JobSettlementRecord  # noqa: E402,F401\n')


def edit_accounts(s: str) -> str:
    s = replace_function(s, 'bind_job', '''def bind_job(db,job_id,connector,reserve_credits=0.0):
    from app.cost_accounting import atomic_bind_job
    return atomic_bind_job(db, job_id, connector, reserve_credits)
''')
    s = replace_function(s, 'settle_job', '''def settle_job(db,job):
    from app.cost_accounting import atomic_settle_job
    return atomic_settle_job(db, job)
''')
    return s


def edit_engine(s: str) -> str:
    # Capture the price at START of a text attempt (not a mutable admin rate after completion).
    before = '''        attempt = JobAttempt(job_id=job.id, route_code=r.code, provider=r.provider, model_name=r.model_name)
        db.add(attempt)
        db.commit()
        started = time.perf_counter()
'''
    after = '''        attempt = JobAttempt(job_id=job.id, route_code=r.code, provider=r.provider, model_name=r.model_name)
        db.add(attempt)
        db.flush()
        from app.cost_accounting import snapshot_attempt_start
        snapshot_attempt_start(db, attempt, r)
        db.commit()
        started = time.perf_counter()
'''
    s = expect_replace(s, before, after, 'app/engine.py text attempt snapshot')

    # Vision provider usage no longer incorrectly books a ZERO USD cost.
    start = s.index('        if op == "image.understand":')
    end = s.index('        raise ValueError("Unsupported media operation")', start)
    region = s[start:end]
    region = expect_replace(region,
        '''            attempt.estimated_cost_usd = 0.0\n''',
        '''            from app.cost_accounting import media_usage_cost
            vision_cost = media_usage_cost(db, "openai", str(result.get("model") or model or "vision-default"), input_tokens, output_tokens)
            attempt.estimated_cost_usd = float(vision_cost or 0.0)
''', 'image understand attempt cost')
    region = expect_replace(region,
        '''            job.estimated_cost_usd = 0.0\n''',
        '''            job.estimated_cost_usd = float(vision_cost or 0.0)
            trace["vision_cost_source"] = ("provider_usage_configured_tariff" if vision_cost is not None
                                          else "unknown_vision_tariff_or_usage")
''', 'image understand job cost')
    s = s[:start] + region + s[end:]

    # Validate the full maximum output budget BEFORE every text provider call.
    # No absolute guarantee of provider usage: token counts and billed extras vary.
    before = '''        if actual_provider_calls > 0 and job.max_cost_usd is not None:
            projected = total_cost + float(selected.estimated_cost_usd or 0.0)
            if projected > float(job.max_cost_usd):
                last_errors.append("recovery_budget_exceeded")
                attempts_trace.append({
                    "route": r.code, "provider": r.provider, "model": r.model_name,
                    "status": "budget_skipped", "projected_cost_usd": round(projected, 8),
                })
                continue
'''
    after = '''        if job.max_cost_usd is not None:
            from app.cost_accounting import conservative_preflight_estimate
            ceiling = conservative_preflight_estimate(r, prompt, context, job.max_output_tokens)
            projected = total_cost + ceiling if ceiling is not None else None
            if projected is None or projected > float(job.max_cost_usd):
                last_errors.append("preflight_budget_exceeded_or_unknown_tariff")
                attempts_trace.append({
                    "route": r.code, "provider": r.provider, "model": r.model_name,
                    "status": "budget_skipped", "projected_cost_usd": (round(projected, 8) if projected is not None else None),
                    "cost_estimate_policy": "conservative, not invoice-guaranteed",
                })
                continue
'''
    s = expect_replace(s, before, after, 'app/engine.py preflight budget')
    return s


def edit_flask(s: str) -> str:
    # Capture both old and new admin rates. Never rewrite a historical version.
    anchor = '''        row = db.get(ModelRoute, model_id)
        if row:
            row.enabled = request.form.get("enabled") == "on"'''
    replacement = '''        row = db.get(ModelRoute, model_id)
        if row:
            from app.cost_accounting import snapshot_route_price
            snapshot_route_price(db, row)
            row.enabled = request.form.get("enabled") == "on"'''
    s = expect_replace(s, anchor, replacement, 'flask_app.py admin tariff before')
    anchor = '''            else:
                row.routing_profile_json = None
            db.commit()
    return redirect("/admin/models", code=303)'''
    replacement = '''            else:
                row.routing_profile_json = None
            snapshot_route_price(db, row)
            db.commit()
    return redirect("/admin/models", code=303)'''
    s = expect_replace(s, anchor, replacement, 'flask_app.py admin tariff after')

    # If connector supplies explicit max_cost_usd, hold full declared budget.
    # If not, conservatively reserve according to existing estimates.
    anchor = '''    if not getattr(g, "connector", None):
        return 0.0
    if payload.operation in {"image.generate", "image_generation.generate"}:'''
    replacement = '''    if not getattr(g, "connector", None):
        return 0.0
    if payload.max_cost_usd is not None:
        return round(float(payload.max_cost_usd) * credits_per_usd(db), 6)
    if payload.operation in {"image.generate", "image_generation.generate"}:'''
    s = expect_replace(s, anchor, replacement, 'flask_app.py reserve explicit budget')

    # In the existing /run and /jobs paths, clean up an orphaned job if a
    # concurrent credit reservation loses the account lock race.
    anchor = '''            bind_job(db, job.id, connector, reserve)
        try:
            job = execute_job(db, job)'''
    replacement = '''            try:
                bind_job(db, job.id, connector, reserve)
            except ValueError:
                db.rollback()
                db.delete(db.get(Job, job.id))
                db.commit()
                return json_error("Insufficient credits after concurrent reservation", 402)
        try:
            job = execute_job(db, job)'''
    s = expect_replace(s, anchor, replacement, 'flask_app.py /run atomic collision')
    anchor = '''            bind_job(db, job.id, connector, reserve)
        return jsonify({"job_id": job.id, "status": job.status}), 201'''
    replacement = '''            try:
                bind_job(db, job.id, connector, reserve)
            except ValueError:
                db.rollback()
                db.delete(db.get(Job, job.id))
                db.commit()
                return json_error("Insufficient credits after concurrent reservation", 402)
        return jsonify({"job_id": job.id, "status": job.status}), 201'''
    s = expect_replace(s, anchor, replacement, 'flask_app.py /jobs atomic collision')

    # One simple, protected JSON report; no modification of existing admin templates.
    anchor = '''@app.get("/admin/jobs")
@require_admin
def admin_jobs():'''
    replacement = '''@app.get("/admin/economics-v2")
@require_admin
def admin_economics_v2():
    from app.cost_accounting import finance_summary
    with SessionLocal() as db:
        return jsonify(finance_summary(db, limit=request.args.get("limit", 100)))


@app.post("/admin/media-tariff")
@require_admin
def admin_media_tariff_v2():
    """Configure a priced image-understanding model; use trusted admin session only."""
    payload = request.get_json(silent=True) or request.form
    provider = str(payload.get("provider") or "").strip().lower()
    model = str(payload.get("model") or "").strip()
    if provider not in {"openai", "anthropic", "gemini", "deepseek"} or not model or len(model) > 60:
        return json_error("Invalid provider or model", 422)
    try:
        inp = float(payload["input_usd_per_million"])
        out = float(payload["output_usd_per_million"])
    except (TypeError, ValueError, KeyError):
        return json_error("Invalid price fields", 422)
    import math
    if not all(math.isfinite(x) and x >= 0 for x in (inp, out)) or inp + out <= 0:
        return json_error("Tariff must have a finite, positive input or output price", 422)
    from app.cost_accounting import _snapshot
    with SessionLocal() as db:
        key = f"media_tariff:{provider}:{model}"
        if len(key) > 100:
            return json_error("Tariff key too long", 422)
        tariff = {"input_usd_per_million": inp, "output_usd_per_million": out}
        version = _snapshot(db, provider, model, "vision", tariff, "operator_configured")
        row = db.get(RuntimeSetting, key)
        if row is None:
            row = RuntimeSetting(key=key, value=json.dumps(tariff))
            db.add(row)
        else:
            row.value = json.dumps(tariff)
        version_id = version.id
        db.commit()
    return jsonify({"ok": True, "version_id": version_id})


@app.get("/admin/jobs")
@require_admin
def admin_jobs():'''
    s = expect_replace(s, anchor, replacement, 'flask_app.py admin economics endpoints')
    return s


EDITORS = {
    'app/models.py': edit_models,
    'app/accounts.py': edit_accounts,
    'app/engine.py': edit_engine,
    'flask_app.py': edit_flask,
}


def source_from_github():
    url = f'https://github.com/soufekladislav/AI-engine/archive/{BASE_COMMIT}.zip'
    print('Stahuji referenční zdroj: ' + url)
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Fresh-AI-Engine-billing-release-builder'}), timeout=40) as response:
        data = response.read(5_000_001)
    if len(data) > 5_000_000:
        raise RuntimeError('Repository archive unexpectedly large')
    directory = Path(tempfile.mkdtemp(prefix='fresh-ai-baseline-'))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            # Only accept listed source files; no arbitrary archive extraction.
            for path in EXPECTED_BLOBS:
                if info.filename.endswith('/' + path):
                    dest = directory / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(info))
    return directory


def build(repo: Path, output: Path, allow_modified=False) -> None:
    prepared = {}
    for rel, fn in EDITORS.items():
        original = (repo / rel).read_bytes()
        blob = git_blob_sha(original)
        if blob != EXPECTED_BLOBS[rel] and not allow_modified:
            raise RuntimeError(f'Source mismatch: {rel}; expected {EXPECTED_BLOBS[rel]}, got {blob}. '
                               'Do not deploy unreviewed patches on a different revision.')
        patched = fn(original.decode('utf-8'))
        compile(patched, rel, 'exec')
        prepared[rel] = patched.encode('utf-8')
    root = Path(__file__).resolve().parent
    for rel in NEW_FILES:
        contents = (root / rel).read_bytes()
        compile(contents, rel, 'exec')
        prepared[rel] = contents
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, contents in prepared.items():
            z.writestr(name, contents)
        z.write(root / 'DEPLOYMENT.md', 'DEPLOYMENT.md')
    print('Hotovo: ' + str(output))
    print('Soubory: ' + ', '.join(prepared))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--repo', type=Path, help='Already downloaded AI-engine repository root')
    source.add_argument('--download', action='store_true', help='Download the pinned public GitHub revision')
    parser.add_argument('--output', type=Path, default=Path('Fresh-AI-Engine-0.8.1-changed-files.zip'))
    args = parser.parse_args()
    repo = source_from_github() if args.download else args.repo
    build(repo, args.output)


if __name__ == '__main__':
    main()
