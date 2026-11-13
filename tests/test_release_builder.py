"""Checks all string-splice anchors produce syntactically valid replacement modules."""
import importlib.util
import pathlib
import sys
import zipfile


def load_builder():
    root=pathlib.Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('fresh_billing_builder',root/'build_release.py')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


def test_splice_helpers_and_zip_from_synthetic_source(tmp_path):
    b=load_builder()
    root=tmp_path/'repo'
    (root/'app').mkdir(parents=True)
    (root/'app/models.py').write_text('from app.db import Base\n', encoding='utf8')
    (root/'app/accounts.py').write_text('''def bind_job(db,job_id,connector,reserve_credits=0.0):
    return None

def settle_job(db,job):
    return None

def charge_job(db,job):
    return settle_job(db,job)
''',encoding='utf8')
    engine = '''def before():
    if True:
        if op == "image.understand":
            model = "x"
            input_tokens = 1
            output_tokens = 1
            result = {}
            attempt.estimated_cost_usd = 0.0
            job.estimated_cost_usd = 0.0
        raise ValueError("Unsupported media operation")

def execute():
    if job.max_cost_usd is not None:
        pass
    for selected in ordered:
        r = selected.route
        if actual_provider_calls > 0 and job.max_cost_usd is not None:
            projected = total_cost + float(selected.estimated_cost_usd or 0.0)
            if projected > float(job.max_cost_usd):
                last_errors.append("recovery_budget_exceeded")
                attempts_trace.append({
                    "route": r.code, "provider": r.provider, "model": r.model_name,
                    "status": "budget_skipped", "projected_cost_usd": round(projected, 8),
                })
                continue
        attempt = JobAttempt(job_id=job.id, route_code=r.code, provider=r.provider, model_name=r.model_name)
        db.add(attempt)
        db.commit()
        started = time.perf_counter()
'''
    (root/'app/engine.py').write_text(engine,encoding='utf8')
    flask = '''def admin_update_model(model_id):
    with SessionLocal() as db:
        row = db.get(ModelRoute, model_id)
        if row:
            row.enabled = request.form.get("enabled") == "on"
            row.quality = 2
            if True:
                row.routing_profile_json = "x"
            else:
                row.routing_profile_json = None
            db.commit()
    return redirect("/admin/models", code=303)

def customer_required_reserve(db, payload):
    if not getattr(g, "connector", None):
        return 0.0
    if payload.operation in {"image.generate", "image_generation.generate"}:
        return 1.0

def execute_payload_sync():
    with SessionLocal() as db:
        if True:
            bind_job(db, job.id, connector, reserve)
        try:
            job = execute_job(db, job)
        except Exception:
            pass

def queue_job():
    with SessionLocal() as db:
        if True:
            bind_job(db, job.id, connector, reserve)
        return jsonify({"job_id": job.id, "status": job.status}), 201

@app.get("/admin/jobs")
@require_admin
def admin_jobs():
    return None
'''
    (root/'flask_app.py').write_text(flask,encoding='utf8')
    output=tmp_path/'build.zip'
    b.build(root,output,allow_modified=True)
    with zipfile.ZipFile(output) as z:
        assert set(z.namelist())==set(b.EDITORS)|set(b.NEW_FILES)|{'DEPLOYMENT.md'}
        for name in b.EDITORS:
            compile(z.read(name),name,'exec')
        assert b'atomic_bind_job' in z.read('app/accounts.py')
        assert b'media_usage_cost' in z.read('app/engine.py')
        assert b'admin_economics_v2' in z.read('flask_app.py')
