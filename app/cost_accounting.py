"""Auditable costs and atomic credit settlement for Fresh AI Engine.

Uses the already existing CustomerAccount row as a per-account transaction lock.
No external AI calls. Never silently interpret an unpriced paid attempt as free.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db import engine
from app.models import (
    CustomerAccount, Job, JobAttempt, JobBilling, CreditLedger,
    ModelRoute, RoutingDecision, RuntimeSetting,
)
from app.cost_models import (
    ProviderTariffVersion, AttemptCostRecord, JobBillingTerms, JobSettlementRecord,
)

D = lambda x: Decimal(str(x or 0))


def rounded(value, places='0.000001'):
    return float(D(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _get_setting(db, key, default=None):
    row = db.get(RuntimeSetting, key)
    try:
        return float(row.value) if row else default
    except (ValueError, TypeError):
        return default


def _snapshot(db, provider, model, modality, pricing, source):
    """Immutable tariff versions. A changed rate creates a new row, never overwrites history."""
    value = json.dumps(pricing, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    fingerprint = hashlib.sha256(value.encode()).hexdigest()
    stmt = select(ProviderTariffVersion).where(
        ProviderTariffVersion.provider == provider,
        ProviderTariffVersion.model_name == model,
        ProviderTariffVersion.modality == modality,
        ProviderTariffVersion.fingerprint == fingerprint,
    )
    row = db.scalar(stmt)
    if row:
        return row
    try:
        with db.begin_nested():
            row = ProviderTariffVersion(provider=provider, model_name=model,
                modality=modality, fingerprint=fingerprint, pricing_json=value,
                price_source=source)
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        row = db.scalar(stmt)
        if row is None:
            raise
        return row


def snapshot_route_price(db, route):
    return _snapshot(db, route.provider, route.model_name, 'text', {
        'input_usd_per_million': float(route.input_usd_per_million),
        'output_usd_per_million': float(route.output_usd_per_million),
    }, 'operator_configured')


def media_tariff(db, provider, model):
    """Configured multimodal token tariffs. Missing price -> unknown, NEVER free."""
    key = f'media_tariff:{provider}:{model}'[:100]
    row = db.get(RuntimeSetting, key)
    if not row:
        return None
    try:
        data = json.loads(row.value)
        if not isinstance(data, dict):
            return None
        inp = float(data['input_usd_per_million'])
        out = float(data['output_usd_per_million'])
        if inp < 0 or out < 0 or not (inp > 0 or out > 0):
            return None
        return {'input_usd_per_million': inp, 'output_usd_per_million': out}
    except (ValueError, TypeError, KeyError):
        return None


def media_usage_cost(db, provider, model, input_tokens, output_tokens):
    if not (int(input_tokens or 0) or int(output_tokens or 0)):
        return None  # Missing usage is not proof of a free provider call.
    pricing = media_tariff(db, provider, model)
    if pricing is None:
        return None
    return rounded((D(input_tokens) * D(pricing['input_usd_per_million']) +
                    D(output_tokens) * D(pricing['output_usd_per_million'])) / D(1000000), '0.00000001')


def _locked_account(db, account_id):
    """Synchronize all debits for one account using the database, not process locks.

    Existing callers may have a read transaction after create_job(). Reset that
    clean transaction before locking to avoid stale InnoDB REPEATABLE READ views.
    """
    # These functions are called immediately after an existing commit; no pending
    # caller changes must be dropped. Do not use this helper from an uncommitted job.
    db.rollback()
    if engine.dialect.name == 'sqlite':
        # Acquires a SQLite write lock, unlike SELECT FOR UPDATE (not supported).
        db.execute(update(CustomerAccount).where(CustomerAccount.id == account_id)
                   .values(active=CustomerAccount.active))
        return db.get(CustomerAccount, account_id)
    return db.scalar(select(CustomerAccount).where(CustomerAccount.id == account_id).with_for_update())


def _balance(db, account_id):
    from sqlalchemy import func
    val = db.scalar(select(func.coalesce(func.sum(CreditLedger.delta_credits), 0)).where(
        CreditLedger.account_id == account_id))
    return D(val)


def atomic_bind_job(db, job_id, connector, reserve_credits=0.0):
    account_id = connector.account_id
    account = _locked_account(db, account_id)
    if account is None or not account.active:
        db.rollback()
        raise ValueError('Account is not available')
    if db.get(JobBilling, job_id):
        db.commit()
        return
    reserve = max(D(0), D(rounded(reserve_credits)))
    if _balance(db, account_id) < reserve:
        db.rollback()
        raise ValueError('Insufficient credits')
    db.add(JobBilling(job_id=job_id, account_id=account_id,
                      connector_id=connector.id, reserved_credits=float(reserve)))
    fx = _get_setting(db, 'usd_czk_reporting_rate', 0)
    db.add(JobBillingTerms(job_id=job_id,
        credits_per_usd=float(_get_setting(db, 'credits_per_usd', 1000)),
        credit_price_czk=float(_get_setting(db, 'credit_price_czk', 0.10)),
        usd_czk_rate=fx if fx and fx > 0 else None,
        reserved_credits=float(reserve)))
    if reserve:
        db.add(CreditLedger(account_id=account_id, delta_credits=-float(reserve),
                            entry_type='reserve', reference=job_id,
                            note='Rezervace pro AI požadavek'))
    db.commit()


def snapshot_attempt_start(db, attempt, route):
    """Capture route rates before the provider call, not after admin edits."""
    tariff = snapshot_route_price(db, route)
    row = db.get(AttemptCostRecord, attempt.id)
    if row is None:
        row = AttemptCostRecord(attempt_id=attempt.id, job_id=attempt.job_id,
            provider=attempt.provider, model_name=attempt.model_name,
            status='started', cost_usd=None, cost_source='awaiting_usage')
        db.add(row)
    row.tariff_version_id = tariff.id
    db.flush()


def _per_attempt_cost(db, attempt, job, trace):
    """Snapshot whatever can be evidenced; unknown cost stays NULL."""
    row = db.get(AttemptCostRecord, attempt.id)
    if row is None:
        row = AttemptCostRecord(attempt_id=attempt.id, job_id=job.id,
                provider=attempt.provider, model_name=attempt.model_name,
                status=attempt.status)
        db.add(row)
    row.status = attempt.status
    row.input_tokens = int(attempt.input_tokens or 0)
    row.output_tokens = int(attempt.output_tokens or 0)
    route = db.scalar(select(ModelRoute).where(ModelRoute.code == attempt.route_code))
    if row.tariff_version_id is None and route is not None:
        row.tariff_version_id = snapshot_route_price(db, route).id
    if job.result_type == 'image' or 'image-generation' in (attempt.route_code or ''):
        source = (trace.get('media_asset') or {}).get('cost_source') or 'image_estimate_unverified'
        # Snapshot the exact code-configured image rate table used by the adapter.
        # This does NOT imply the provider invoice has been reconciled.
        if attempt.provider == 'openai':
            from app.multimodal import OPENAI_IMAGE_TOKEN_RATES, _PER_IMAGE_ESTIMATES, _model_family
            family = _model_family(attempt.model_name)
            price_configuration = {
                'family': family,
                'token_rates_per_million': OPENAI_IMAGE_TOKEN_RATES.get(family),
                'per_image_estimates': _PER_IMAGE_ESTIMATES.get(family),
                'chosen_quality': (trace.get('media_asset') or {}).get('quality'),
                'chosen_size': (trace.get('media_asset') or {}).get('requested_size'),
            }
            row.tariff_version_id = _snapshot(db, attempt.provider, attempt.model_name,
                'image', price_configuration, 'adapter_configured_unverified').id
        # A priced image can have no token usage when provider exposes only a flat image price.
        if attempt.status in {'success','invalid_output'} and (float(attempt.estimated_cost_usd or 0)>0):
            row.cost_usd = float(attempt.estimated_cost_usd)
            row.cost_source = source
        else:
            row.cost_usd = None
            row.cost_source = 'unknown_image_cost'
    elif 'image-understanding' in (attempt.route_code or ''):
        pricing = media_tariff(db, attempt.provider, attempt.model_name)
        if pricing is not None and (row.input_tokens or row.output_tokens):
            row.tariff_version_id = _snapshot(db, attempt.provider, attempt.model_name, 'vision',
                                                pricing, 'operator_configured').id
            row.cost_usd = media_usage_cost(db, attempt.provider, attempt.model_name,
                                            row.input_tokens, row.output_tokens)
            row.cost_source = 'provider_usage_configured_vision_tariff'
        else:
            row.cost_usd = None
            row.cost_source = 'unknown_vision_tariff_or_usage'
    elif attempt.provider == 'mock':
        row.cost_usd = 0.0
        row.cost_source = 'mock_no_provider_charge'
    elif row.input_tokens or row.output_tokens:
        # An immutable attempt-time tariff wins even if admin later disables or
        # changes the model route (including updating prices to zero).
        tariff = db.get(ProviderTariffVersion, row.tariff_version_id) if row.tariff_version_id else None
        pricing = json.loads(tariff.pricing_json) if tariff else (
            {'input_usd_per_million':route.input_usd_per_million,
             'output_usd_per_million':route.output_usd_per_million}
            if route is not None else None)
        if pricing and (float(pricing.get('input_usd_per_million') or 0) > 0 or
                        float(pricing.get('output_usd_per_million') or 0) > 0):
            reconstructed = rounded((D(row.input_tokens) * D(pricing['input_usd_per_million']) +
                D(row.output_tokens) * D(pricing['output_usd_per_million'])) / D(1000000), '0.00000001')
            row.cost_usd = reconstructed
            row.cost_source = 'provider_usage_tariff_snapshot'
        else:
            row.cost_usd = None
            row.cost_source = 'unknown_route_tariff'
    else:
        # A provider may charge for a timed-out request: never record zero as verified.
        row.cost_usd = None
        row.cost_source = 'unknown_no_usage'
    return row


def summarize_attempt_costs(db, job):
    attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)
                          .order_by(JobAttempt.id)).all()
    decision = db.get(RoutingDecision, job.id)
    try:
        trace = json.loads(decision.trace_json) if decision else {}
    except (ValueError, TypeError):
        trace = {}
    records = [_per_attempt_cost(db, attempt, job, trace) for attempt in attempts]
    db.flush()
    known = sum((D(x.cost_usd) for x in records if x.cost_usd is not None), D(0))
    unknown = sum(1 for x in records if x.cost_usd is None)
    return known, unknown


def atomic_settle_job(db, job):
    """Exactly once, including zero-charge and failed jobs.

    All reservation releases, debits and settlement records share the locked
    transaction. When a cost exceeds available credit, NEVER create a negative
    balance; record the uncovered liability as a platform subsidy.
    """
    bill = db.get(JobBilling, job.id)
    if bill is None:
        # Keep attempt evidence for internal or non-connector workloads too.
        summarize_attempt_costs(db, job)
        db.commit()
        return
    account_id = bill.account_id
    if _locked_account(db, account_id) is None:
        raise ValueError('Missing billing account')
    bill = db.get(JobBilling, job.id)
    existing = db.get(JobSettlementRecord, job.id)
    if existing:
        db.commit()
        return
    terms = db.get(JobBillingTerms, job.id)
    reserve = D(bill.reserved_credits)
    known, unknown = summarize_attempt_costs(db, job)
    # Legacy completed jobs can have settled charges without an explicit marker.
    old_settlement = db.scalar(select(CreditLedger.id).where(
        CreditLedger.account_id == account_id,
        CreditLedger.reference == job.id,
        CreditLedger.entry_type == 'settlement').limit(1))
    already_paid = old_settlement is not None or D(bill.credits_charged) > 0
    factor = D(terms.credits_per_usd if terms else _get_setting(db, 'credits_per_usd', 1000))
    requested_charge = max(D(0), D(job.estimated_cost_usd) * factor) if job.status == 'completed' else D(0)
    if already_paid:
        charged = D(bill.credits_charged)
    else:
        # A reservation is an absolute, per-job customer debit ceiling.
        # Never silently consume additional credits beyond what was reserved.
        # Insufficient estimates are borne by the platform and shown as subsidy.
        charged = min(requested_charge, reserve)
        delta = rounded(reserve - charged)
        if delta:
            db.add(CreditLedger(account_id=account_id, delta_credits=delta,
                entry_type='settlement', reference=job.id,
                note='Vyrovnání rezervace AI požadavku'))
        bill.credits_charged = rounded(charged)
        bill.cost_usd = float(job.estimated_cost_usd or 0) if job.status == 'completed' else 0.0
        bill.credit_unit_price_czk = float(terms.credit_price_czk) if terms else float(_get_setting(db, 'credit_price_czk', 0.10))
        bill.usd_czk_rate = terms.usd_czk_rate if terms else _get_setting(db, 'usd_czk_reporting_rate', 0)
        route = db.scalar(select(ModelRoute).where(ModelRoute.code == job.selected_route_code)) if job.selected_route_code else None
        if route:
            bill.input_price_usd_per_million = float(route.input_usd_per_million)
            bill.output_price_usd_per_million = float(route.output_usd_per_million)
    unit_price = D(terms.credit_price_czk if terms else bill.credit_unit_price_czk or _get_setting(db,'credit_price_czk',0.10))
    fx = D(terms.usd_czk_rate if terms else bill.usd_czk_rate)
    revenue = charged * unit_price
    margin = revenue - known * fx if unknown == 0 and fx > 0 else None
    db.add(JobSettlementRecord(
        job_id=job.id, account_id=account_id, outcome=job.status,
        provider_known_cost_usd=rounded(known, '0.00000001'),
        provider_unknown_attempts=unknown,
        charged_credits=rounded(charged),
        platform_subsidy_credits=rounded(max(D(0), requested_charge - charged)),
        nominal_revenue_czk=rounded(revenue),
        gross_margin_czk=rounded(margin) if margin is not None else None,
    ))
    db.commit()


def finance_summary(db, limit=100):
    """Only nominal margin backed by a complete cost estimate, not provider invoices."""
    rows = db.scalars(select(JobSettlementRecord).order_by(
        JobSettlementRecord.settlement_time.desc()).limit(max(1,min(int(limit),1000)))).all()
    attempts = db.scalars(select(AttemptCostRecord).order_by(
        AttemptCostRecord.attempt_id.desc()).limit(max(1,min(int(limit)*4,4000)))).all()
    unbilled = db.execute(select(AttemptCostRecord.cost_usd).outerjoin(
        JobBilling, JobBilling.job_id == AttemptCostRecord.job_id).where(
        JobBilling.job_id.is_(None))).all()
    internal_known_cost = sum((D(value) for (value,) in unbilled if value is not None), D(0))
    internal_unknown = sum(1 for (value,) in unbilled if value is None)
    return {
        'disclaimer':'Odhady z usage API a uložených tarifů; nikoliv odsouhlasené faktury poskytovatelů.',
        'job_count':len(rows),
        'nominal_revenue_czk':rounded(sum(D(r.nominal_revenue_czk) for r in rows)),
        'provider_known_cost_usd':rounded(sum(D(r.provider_known_cost_usd) for r in rows),'0.00000001'),
        'unknown_provider_attempts':sum(r.provider_unknown_attempts for r in rows),
        'margin_known_jobs':sum(r.gross_margin_czk is not None for r in rows),
        'gross_margin_czk_known_jobs_only':rounded(sum(D(r.gross_margin_czk) for r in rows if r.gross_margin_czk is not None)),
        'platform_subsidy_credits':rounded(sum(D(r.platform_subsidy_credits) for r in rows)),
        'unbilled_internal_known_cost_usd_all_time': rounded(internal_known_cost,'0.00000001'),
        'unbilled_internal_unknown_attempts_all_time': internal_unknown,
        'jobs':[{'job_id':r.job_id,'account_id':r.account_id,'outcome':r.outcome,
                 'provider_cost_usd':r.provider_known_cost_usd,
                 'unknown_attempts':r.provider_unknown_attempts,'charged_credits':r.charged_credits,
                 'margin_czk':r.gross_margin_czk,
                 'platform_subsidy_credits':r.platform_subsidy_credits} for r in rows],
        'attempts':[{'attempt_id':r.attempt_id,'job_id':r.job_id,'provider':r.provider,
                     'model':r.model_name,'status':r.status,'cost_usd':r.cost_usd,
                     'cost_source':r.cost_source,'tariff_version_id':r.tariff_version_id} for r in attempts],
    }


def conservative_preflight_estimate(route, prompt: str, context: dict, max_output_tokens: int):
    """Conservative price preflight; NOT a promise about provider invoice totals.

    Uses max allowed output rather than router's typical output heuristic. Final
    provider tokenization and provider-side extras can still differ.
    """
    try:
        inp = float(route.input_usd_per_million)
        out = float(route.output_usd_per_million)
        if inp < 0 or out < 0 or (inp == 0 and out == 0):
            return None
        payload = json.dumps(context, ensure_ascii=False, separators=(',', ':')) if context else ''
        estimated_input = max(1, (len(prompt or '') + len(payload)) / 3.0)
        estimated_input *= 1.6
        return float((D(estimated_input) * D(inp) +
                      D(max_output_tokens) * D(out)) / D(1000000))
    except (ValueError, TypeError, OverflowError):
        return None
