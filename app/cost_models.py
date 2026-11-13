"""Append-only AI tariff and accounting records, introduced in Fresh AI Engine 0.8.1.

Imported by app.models so existing Base.metadata.create_all() creates tables on both
Flask/Passenger and the cron worker. Existing tables and history remain untouched.
"""
from datetime import datetime, timezone
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


class ProviderTariffVersion(Base):
    __tablename__ = 'provider_tariff_versions'
    __table_args__ = (UniqueConstraint('provider', 'model_name', 'modality', 'fingerprint', name='uq_provider_tariff_version'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model_name: Mapped[str] = mapped_column(String(120))
    modality: Mapped[str] = mapped_column(String(30), default='text')
    fingerprint: Mapped[str] = mapped_column(String(64))
    pricing_json: Mapped[str] = mapped_column(Text)
    price_source: Mapped[str] = mapped_column(String(60), default='operator_configured')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttemptCostRecord(Base):
    __tablename__ = 'attempt_cost_records'
    attempt_id: Mapped[int] = mapped_column(ForeignKey('job_attempts.id', ondelete='CASCADE'), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), index=True)
    tariff_version_id: Mapped[int | None] = mapped_column(ForeignKey('provider_tariff_versions.id'), nullable=True)
    provider: Mapped[str] = mapped_column(String(40))
    model_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30))
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_source: Mapped[str] = mapped_column(String(60), default='unknown')
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class JobBillingTerms(Base):
    __tablename__ = 'job_billing_terms'
    job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True)
    credits_per_usd: Mapped[float] = mapped_column(Float)
    credit_price_czk: Mapped[float] = mapped_column(Float)
    usd_czk_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    reserved_credits: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobSettlementRecord(Base):
    __tablename__ = 'job_settlement_records'
    job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    provider_known_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    provider_unknown_attempts: Mapped[int] = mapped_column(Integer, default=0)
    charged_credits: Mapped[float] = mapped_column(Float, default=0)
    platform_subsidy_credits: Mapped[float] = mapped_column(Float, default=0)
    nominal_revenue_czk: Mapped[float] = mapped_column(Float, default=0)
    gross_margin_czk: Mapped[float | None] = mapped_column(Float, nullable=True)
    settlement_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
