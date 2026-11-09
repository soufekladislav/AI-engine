"""Nominal credit economics. Never present estimates as provider invoices or paid revenue.

Amounts are based on billed (completed) jobs. Legacy rows without price snapshots
are valued at the current informative credit price/rate and flagged as such.
"""
from __future__ import annotations

from sqlalchemy import select
from app.models import Job, JobBilling, Connector, ModelRoute
from app.accounts import credit_price_czk, credit_setting, balance


def _empty():
    return dict(jobs=0, input_tokens=0, output_tokens=0, credits=0.0,
                nominal_czk=0.0, cost_usd=0.0, cost_czk=0.0, legacy_jobs=0,
                missing_rates=0, providers=[], profit_czk=None, margin_pct=None)


def _aggregate(row, record, unit_price, fx_rate):
    job, bill, route = record
    if job.status != 'completed':
        return
    row['jobs'] += 1
    row['input_tokens'] += int(job.input_tokens or 0)
    row['output_tokens'] += int(job.output_tokens or 0)
    credits = max(0.0, float(bill.credits_charged or 0))
    cost = max(0.0, float(bill.cost_usd or 0))
    row['credits'] += credits
    row['cost_usd'] += cost
    if bill.credit_unit_price_czk is None:
        row['legacy_jobs'] += 1
    row['nominal_czk'] += credits * (unit_price if bill.credit_unit_price_czk is None else float(bill.credit_unit_price_czk))
    effective_fx = bill.usd_czk_rate if bill.usd_czk_rate is not None else fx_rate
    if effective_fx > 0:
        row['cost_czk'] += cost * effective_fx
    elif cost > 0:
        row['missing_rates'] += 1
    if job.provider not in {'local', 'mock'} and cost == 0:
        row['missing_rates'] += 1


def _finish(row):
    if not row['missing_rates']:
        row['profit_czk'] = row['nominal_czk'] - row['cost_czk']
        row['margin_pct'] = (100 * row['profit_czk'] / row['nominal_czk']) if row['nominal_czk'] > 0 else None
    return row


def account_finance(db, account_id):
    price = credit_price_czk(db)
    fx = credit_setting(db, 'usd_czk_reporting_rate', 0.0)
    fx = fx if fx > 0 else 0.0
    rows = db.execute(select(Job, JobBilling, Connector, ModelRoute)
        .join(JobBilling, JobBilling.job_id == Job.id)
        .outerjoin(Connector, Connector.id == JobBilling.connector_id)
        .outerjoin(ModelRoute, ModelRoute.code == Job.selected_route_code)
        .where(JobBilling.account_id == account_id)
        .order_by(Job.created_at.desc())).all()
    summary = _empty()
    projects = {}
    for job, bill, connector, route in rows:
        if job.status != 'completed':
            continue
        rec = (job, bill, route)
        _aggregate(summary, rec, price, fx)
        key = (job.project_id or '', job.source or '', bill.connector_id or '')
        project = projects.setdefault(key, {**_empty(), 'project':job.project_id or 'Bez označení projektu',
            'source':job.source or '—', 'connector':connector.name if connector else 'Smazaný konektor',
            'connector_id':bill.connector_id or '', '_providers':{}})
        _aggregate(project, rec, price, fx)
        provider_key = (job.provider or 'nezjištěno', job.model_name or '—', job.selected_route_code or '')
        provider = project['_providers'].setdefault(provider_key, {**_empty(),
            'provider':provider_key[0], 'model':provider_key[1], 'route':provider_key[2],
            'input_rate':None, 'output_rate':None, 'legacy_rate':False})
        _aggregate(provider, rec, price, fx)
        if bill.input_price_usd_per_million is not None and bill.output_price_usd_per_million is not None:
            if provider['input_rate'] is None:
                provider['input_rate'] = float(bill.input_price_usd_per_million)
                provider['output_rate'] = float(bill.output_price_usd_per_million)
            elif (provider['input_rate'] != float(bill.input_price_usd_per_million) or
                  provider['output_rate'] != float(bill.output_price_usd_per_million)):
                provider['legacy_rate'] = True  # Different tariffs used over this period.
        else:
            provider['legacy_rate'] = True
            if route is not None:
                provider['input_rate'] = float(route.input_usd_per_million)
                provider['output_rate'] = float(route.output_usd_per_million)
    for project in projects.values():
        project['providers'] = sorted((_finish(p) for p in project.pop('_providers').values()),
                                      key=lambda r: (-r['cost_usd'],r['provider']))
        _finish(project)
    summary = _finish(summary)
    current_balance = balance(db, account_id)
    summary.update(balance_credits=current_balance, balance_czk=current_balance*price,
                   credit_price_czk=price, fx_rate=fx,
                   project_count=len(projects))
    return summary, sorted(projects.values(), key=lambda r:(-r['cost_usd'],r['project'],r['source']))


def accounts_finance_overview(db, account_ids):
    """Single-pass sums for admin account list; only completed booked jobs."""
    from sqlalchemy import func
    from app.models import CreditLedger
    ids = list(account_ids)
    if not ids: return {}
    price=credit_price_czk(db)
    fx=credit_setting(db, 'usd_czk_reporting_rate', 0.0)
    items={key:_empty() for key in ids}
    for job,bill,route in db.execute(select(Job,JobBilling,ModelRoute)
        .join(JobBilling,JobBilling.job_id==Job.id)
        .outerjoin(ModelRoute,ModelRoute.code==Job.selected_route_code)
        .where(JobBilling.account_id.in_(ids),Job.status=='completed')).all():
        _aggregate(items[bill.account_id],(job,bill,route),price,fx)
    balances=dict(db.execute(select(CreditLedger.account_id,func.coalesce(func.sum(CreditLedger.delta_credits),0))
        .where(CreditLedger.account_id.in_(ids)).group_by(CreditLedger.account_id)).all())
    for key,row in items.items():
        _finish(row)
        row['balance_credits']=float(balances.get(key) or 0)
        row['balance_czk']=row['balance_credits']*price
    return items
