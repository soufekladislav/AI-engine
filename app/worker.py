import argparse
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.config import get_settings
from app.db import Base, engine, SessionLocal
from app.engine import execute_job, is_queue_paused, fail_job_safely
from app.models import Job
from app.seed import seed_defaults
from app.schema_upgrade import ensure_schema_upgrades
from app.fx_cnb import refresh_cnb_fx


def claim_one(db):
    stmt = select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1)
    # SQLite has no SKIP LOCKED. MariaDB/MySQL in supported versions does.
    if not str(engine.url).startswith("sqlite"):
        stmt = stmt.with_for_update(skip_locked=True)
    job = db.scalar(stmt)
    if not job:
        return None
    job.status = "claimed"
    db.commit()
    return job




def recover_stale_jobs(db, timeout_seconds: int) -> int:
    """Close abandoned claimed/running jobs instead of leaving them forever.

    We deliberately do not auto-retry them: a provider could have received the
    original request before the worker died. Marking failed avoids duplicate
    paid calls and releases any outstanding reservation via settlement.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(60, int(timeout_seconds or 180)))
    rows = db.scalars(select(Job).where(Job.status.in_(["claimed", "running"]))).all()
    recovered = 0
    for job in rows:
        stamp = job.started_at or job.created_at
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp > cutoff:
            continue
        fail_job_safely(db, job, "stale_job_recovered", None, {"provider_calls": 0})
        recovered += 1
    return recovered

def process_batch(max_jobs: int | None = None) -> int:
    settings = get_settings()
    Base.metadata.create_all(engine)
    ensure_schema_upgrades(engine)
    with SessionLocal() as db:
        seed_defaults(db)
        refresh_cnb_fx(db)
        recover_stale_jobs(db, settings.job_timeout_seconds)

    limit = max_jobs or settings.worker_batch_size
    processed = 0
    for _ in range(max(1, limit)):
        with SessionLocal() as db:
            if is_queue_paused(db):
                break
            job = claim_one(db)
            if not job:
                break
            try:
                execute_job(db, job)
            except Exception as exc:
                fail_job_safely(db, job, "worker_internal_error", exc, {"provider_calls": 0})
            processed += 1
    return processed


def main():
    parser = argparse.ArgumentParser(description="Fresh AI cPanel queue worker")
    parser.add_argument("--max-jobs", type=int, default=None)
    args = parser.parse_args()
    count = process_batch(args.max_jobs)
    print(f"Fresh AI worker processed {count} job(s)")


if __name__ == "__main__":
    main()
