"""Isolated SQLAlchemy safety tests. Run with: pytest -q tests/

The source checkout is injected as an app package when deployed. These fixture
models deliberately reproduce just the existing fields needed for accounting.
"""
import importlib
import sys
import types
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, String, Integer, Float, Boolean, ForeignKey, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Dedicated fake app package, loaded from the proposed upgrade folder.
    for name in list(sys.modules):
        if name == 'app' or name.startswith('app.'):
            del sys.modules[name]
    path = Path(__file__).resolve().parents[1] / 'app'
    package = types.ModuleType('app')
    package.__path__ = [str(path)]
    sys.modules['app'] = package
    dbmod = types.ModuleType('app.db')
    class Base(DeclarativeBase): pass
    engine = create_engine('sqlite:///' + str(tmp_path / 'billing.db'), connect_args={'timeout':10})
    dbmod.Base = Base
    dbmod.engine = engine
    sys.modules['app.db'] = dbmod
    models = types.ModuleType('app.models')

    class CustomerAccount(Base):
        __tablename__ = 'customer_accounts'
        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        active: Mapped[bool] = mapped_column(Boolean, default=True)

    class Job(Base):
        __tablename__ = 'jobs'
        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        status: Mapped[str] = mapped_column(String(20), default='queued')
        result_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
        selected_route_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
        estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)

    class JobAttempt(Base):
        __tablename__ = 'job_attempts'
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id'))
        route_code: Mapped[str] = mapped_column(String(80))
        provider: Mapped[str] = mapped_column(String(40))
        model_name: Mapped[str] = mapped_column(String(120))
        status: Mapped[str] = mapped_column(String(20), default='started')
        input_tokens: Mapped[int] = mapped_column(Integer, default=0)
        output_tokens: Mapped[int] = mapped_column(Integer, default=0)
        estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)

    class CreditLedger(Base):
        __tablename__ = 'credit_ledger'
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        account_id: Mapped[str] = mapped_column(ForeignKey('customer_accounts.id'))
        delta_credits: Mapped[float] = mapped_column(Float)
        entry_type: Mapped[str] = mapped_column(String(30))
        reference: Mapped[str] = mapped_column(String(100), default='')
        note: Mapped[str] = mapped_column(Text, default='')

    class JobBilling(Base):
        __tablename__ = 'job_billing'
        job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id'), primary_key=True)
        account_id: Mapped[str] = mapped_column(ForeignKey('customer_accounts.id'))
        connector_id: Mapped[str] = mapped_column(String(36))
        reserved_credits: Mapped[float] = mapped_column(Float, default=0)
        credits_charged: Mapped[float] = mapped_column(Float, default=0)
        cost_usd: Mapped[float] = mapped_column(Float, default=0)
        credit_unit_price_czk: Mapped[float | None] = mapped_column(Float, nullable=True)
        usd_czk_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
        input_price_usd_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
        output_price_usd_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)

    class ModelRoute(Base):
        __tablename__ = 'model_routes'
        code: Mapped[str] = mapped_column(String(80), primary_key=True)
        provider: Mapped[str] = mapped_column(String(40))
        model_name: Mapped[str] = mapped_column(String(120))
        input_usd_per_million: Mapped[float] = mapped_column(Float)
        output_usd_per_million: Mapped[float] = mapped_column(Float)

    class RoutingDecision(Base):
        __tablename__ = 'routing_decisions'
        job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id'), primary_key=True)
        trace_json: Mapped[str] = mapped_column(Text, default='{}')

    class RuntimeSetting(Base):
        __tablename__ = 'runtime_settings'
        key: Mapped[str] = mapped_column(String(100), primary_key=True)
        value: Mapped[str] = mapped_column(Text)

    for cls in [CustomerAccount,Job,JobAttempt,CreditLedger,JobBilling,ModelRoute,RoutingDecision,RuntimeSetting]:
        setattr(models, cls.__name__, cls)
    sys.modules['app.models'] = models
    cost = importlib.import_module('app.cost_accounting')
    from app.cost_models import JobSettlementRecord, AttemptCostRecord, ProviderTariffVersion
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        db.add(CustomerAccount(id='acct'))
        db.add(CreditLedger(account_id='acct',delta_credits=1000,entry_type='topup',reference='top'))
        db.add(ModelRoute(code='mini', provider='openai',model_name='mini-x',input_usd_per_million=1,output_usd_per_million=2))
        db.add(RuntimeSetting(key='usd_czk_reporting_rate',value='23.0'))
        db.commit()
    connector = types.SimpleNamespace(account_id='acct',id='con1')
    return types.SimpleNamespace(cost=cost,Session=Session,models=models,connector=connector,
        AttemptCostRecord=AttemptCostRecord,JobSettlementRecord=JobSettlementRecord,
        Tariff=ProviderTariffVersion)


def test_atomic_concurrent_reservations(env):
    with env.Session() as db:
        db.add_all([env.models.Job(id='j1'),env.models.Job(id='j2')]);db.commit()
    def go(job_id):
        with env.Session() as db:
            try:
                env.cost.atomic_bind_job(db,job_id,env.connector,800)
                return True
            except ValueError:
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(go,['j1','j2']))
    assert sorted(outcomes) == [False,True]
    with env.Session() as db:
        assert env.cost._balance(db,'acct') == 200


def test_settlement_idempotent_and_failed_cost(env):
    with env.Session() as db:
        job=env.models.Job(id='good',status='completed',estimated_cost_usd=0.5,selected_route_code='mini')
        db.add(job);db.commit()
        env.cost.atomic_bind_job(db,'good',env.connector,600)
        db.add(env.models.JobAttempt(job_id='good',route_code='mini',provider='openai',model_name='mini-x',
                                     status='success',input_tokens=10000,output_tokens=5000,estimated_cost_usd=.02))
        db.commit()
        env.cost.atomic_settle_job(db,job)
        assert env.cost._balance(db,'acct') == 500
        env.cost.atomic_settle_job(db,job)
        assert env.cost._balance(db,'acct') == 500
        settlement=db.get(env.JobSettlementRecord,'good')
        assert settlement.provider_known_cost_usd == pytest.approx(.02)
        assert settlement.gross_margin_czk == pytest.approx(49.54)
        assert settlement.charged_credits==500


def test_failed_attempt_is_platform_cost_not_customer_charge(env):
    with env.Session() as db:
        job=env.models.Job(id='failed',status='failed',estimated_cost_usd=.03,selected_route_code='mini')
        db.add(job);db.commit()
        env.cost.atomic_bind_job(db,'failed',env.connector,200)
        db.add(env.models.JobAttempt(job_id='failed',route_code='mini',provider='openai',model_name='mini-x',
                                     status='invalid_output',input_tokens=10000,output_tokens=10000,estimated_cost_usd=.03))
        db.commit()
        env.cost.atomic_settle_job(db,job)
        assert env.cost._balance(db,'acct') == 1000
        row=db.get(env.JobSettlementRecord,'failed')
        assert row.charged_credits == 0
        assert row.gross_margin_czk == pytest.approx(-.69)


def test_vision_unknown_tariff_is_not_free(env):
    with env.Session() as db:
        job=env.models.Job(id='vision',status='completed',result_type='text',selected_route_code='openai-image-understanding')
        db.add(job);db.commit()
        env.cost.atomic_bind_job(db,'vision',env.connector,0)
        attempt=env.models.JobAttempt(job_id='vision',route_code='openai-image-understanding',provider='openai',
                                     model_name='vision-v1',status='success',input_tokens=500,output_tokens=200)
        db.add(attempt);db.commit()
        env.cost.atomic_settle_job(db,job)
        rec=db.get(env.AttemptCostRecord,attempt.id)
        assert rec.cost_usd is None
        assert db.get(env.JobSettlementRecord,'vision').gross_margin_czk is None


def test_tariff_versions_immutable(env):
    with env.Session() as db:
        route=db.get(env.models.ModelRoute,'mini')
        older=env.cost.snapshot_route_price(db,route)
        older_id=older.id
        route.input_usd_per_million=10.0
        newer=env.cost.snapshot_route_price(db,route)
        db.commit()
        assert older_id!=newer.id
        assert len(db.scalars(select(env.Tariff)).all())==2
        assert '"input_usd_per_million":1.0' in db.get(env.Tariff,older_id).pricing_json


def test_preflight_output_ceiling(env):
    with env.Session() as db:
        route=db.get(env.models.ModelRoute,'mini')
        assert env.cost.conservative_preflight_estimate(route,'abc',{},10000)>0.02


def test_overage_never_goes_negative(env):
    with env.Session() as db:
        job=env.models.Job(id='over',status='completed',estimated_cost_usd=2.0,selected_route_code='mini')
        db.add(job);db.commit()
        env.cost.atomic_bind_job(db,'over',env.connector,300)
        db.add(env.models.JobAttempt(job_id='over',route_code='mini',provider='openai',model_name='mini-x',
                                     status='success',input_tokens=10000,output_tokens=10000,estimated_cost_usd=.03))
        db.commit()
        env.cost.atomic_settle_job(db,job)
        assert env.cost._balance(db,'acct') == 700
        settlement=db.get(env.JobSettlementRecord,'over')
        assert settlement.charged_credits == 300
        assert settlement.platform_subsidy_credits == 1700
