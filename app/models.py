from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT, LONGBLOB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


class ModelRoute(Base):
    __tablename__ = "model_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model_name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quality: Mapped[int] = mapped_column(Integer, default=3)  # 1..5
    priority: Mapped[int] = mapped_column(Integer, default=50)  # larger = preferred
    capabilities: Mapped[str] = mapped_column(String(250), default="text,general,chat")
    routing_profile_json: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    input_usd_per_million: Mapped[float] = mapped_column(Float, default=0.0)
    output_usd_per_million: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    source: Mapped[str] = mapped_column(String(80), default="unknown", index=True)
    project_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    task_type: Mapped[str] = mapped_column(String(50), default="general", index=True)
    operation: Mapped[str | None] = mapped_column(String(160), nullable=True)
    quality: Mapped[str] = mapped_column(String(20), default="auto")
    max_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=1200)

    prompt_hash: Mapped[str] = mapped_column(String(64))
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    prompt_payload: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    context_payload: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    input_data_payload: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    output_contract_payload: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)

    selected_route_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    response_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    result_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    result_schema_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    result_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    routing_decision: Mapped[RoutingDecision | None] = relationship(cascade="all, delete-orphan", uselist=False)
    attempts: Mapped[list[JobAttempt]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobAttempt(Base):
    __tablename__ = "job_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    route_code: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="started")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="attempts")


class JobFeedback(Base):
    __tablename__ = "job_feedback"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RoutingDecision(Base):
    __tablename__ = "routing_decisions"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    trace_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class CapabilityDefinition(Base):
    __tablename__ = "capability_definitions"
    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    phrases_json: Mapped[str] = mapped_column(Text, default="[]")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DiagnosticShare(Base):
    __tablename__ = "diagnostic_shares"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default="current")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    manifest_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    bundle_zip: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB(), 'mysql'), nullable=False)

class CustomerAccount(Base):
    __tablename__ = "customer_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    company_name: Mapped[str] = mapped_column(String(190), default="")
    billing_name: Mapped[str] = mapped_column(String(190), default="")
    billing_ico: Mapped[str] = mapped_column(String(40), default="")
    billing_dic: Mapped[str] = mapped_column(String(40), default="")
    billing_address: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Connector(Base):
    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(24), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatUsageUser(Base):
    """Admin-only minimal directory, updated only by authenticated Fresh Chat service."""
    __tablename__ = 'chat_usage_users'
    chat_user_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    fresh_user_id: Mapped[str] = mapped_column(String(120), default='', index=True)
    display_name: Mapped[str] = mapped_column(String(160), default='')
    email: Mapped[str] = mapped_column(String(190), default='')
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CreditLedger(Base):
    __tablename__ = "credit_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), index=True)
    delta_credits: Mapped[float] = mapped_column(Float, default=0.0)
    entry_type: Mapped[str] = mapped_column(String(40), index=True)
    reference: Mapped[str] = mapped_column(String(120), default="", index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class CreditOrder(Base):
    __tablename__ = "credit_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), index=True)
    credits: Mapped[float] = mapped_column(Float)
    amount_czk: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    payment_reference: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobBilling(Base):
    __tablename__ = "job_billing"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), index=True)
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), index=True)
    reserved_credits: Mapped[float] = mapped_column(Float, default=0.0)
    credits_charged: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Nullable snapshots: legacy bills were booked before these fields existed.
    credit_unit_price_czk: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_czk_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_price_usd_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price_usd_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccountEmailVerification(Base):
    __tablename__ = "account_email_verifications"
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class AccountPasswordReset(Base):
    __tablename__ = "account_password_resets"
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), default="freshcrackers", index=True)
    external_user_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class ExternalOrder(Base):
    __tablename__ = "external_orders"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("customer_accounts.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="woocommerce", index=True)
    external_order_id: Mapped[str] = mapped_column(String(80), index=True)
    product_code: Mapped[str] = mapped_column(String(100), default="")
    action_type: Mapped[str] = mapped_column(String(40), default="credit")
    action_value: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(12), default="CZK")
    status: Mapped[str] = mapped_column(String(40), default="completed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SupportSubmission(Base):
    """Minimal audit/rate-limit metadata; support message is sent via SMTP only."""
    __tablename__ = 'support_submissions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    email_hash: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reference: Mapped[str] = mapped_column(String(24), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AccountTrash(Base):
    """Deletion workflow metadata; an anonymized account tombstone preserves billing FKs."""
    __tablename__ = 'account_trash'
    account_id: Mapped[str] = mapped_column(ForeignKey('customer_accounts.id', ondelete='CASCADE'), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminAccountAudit(Base):
    __tablename__ = 'admin_account_audit'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    details: Mapped[str] = mapped_column(String(255), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccountInvitation(Base):
    """A one-use onboarding link; invitations create a local account only."""
    __tablename__ = 'account_invitations'
    account_id: Mapped[str] = mapped_column(ForeignKey('customer_accounts.id', ondelete='CASCADE'), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccountAdminState(Base):
    __tablename__ = 'account_admin_state'
    account_id: Mapped[str] = mapped_column(ForeignKey('customer_accounts.id', ondelete='CASCADE'), primary_key=True)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Fresh Key: shared identity/project/connection layer.  Work data stays in the
# individual Fresh applications; these tables only hold identity, project-link graph
# and external authorizations shared by those applications.
# ---------------------------------------------------------------------------

class FreshKeyAppBinding(Base):
    __tablename__ = "fresh_key_app_bindings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), unique=True, index=True)
    app_code: Mapped[str] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FreshKeyIdentity(Base):
    __tablename__ = "fresh_key_identities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(190), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    engine_account_id: Mapped[str | None] = mapped_column(ForeignKey("customer_accounts.id", ondelete="SET NULL"), nullable=True, unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyIdentityBinding(Base):
    __tablename__ = "fresh_key_identity_bindings"
    __table_args__ = (UniqueConstraint("app_code", "external_user_id", name="uq_fk_identity_binding_app_user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    app_code: Mapped[str] = mapped_column(String(80), index=True)
    external_user_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FreshKeyProject(Base):
    __tablename__ = "fresh_key_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(190), index=True)
    organization_name: Mapped[str] = mapped_column(String(190), default="")
    website_url: Mapped[str] = mapped_column(String(500), default="")
    owner_identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="RESTRICT"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyProjectMember(Base):
    __tablename__ = "fresh_key_project_members"
    __table_args__ = (UniqueConstraint("project_id", "identity_id", name="uq_fk_project_member"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_projects.id", ondelete="CASCADE"), index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="member")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    can_manage_connections: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FreshKeyProjectAppMember(Base):
    """Informational mirror of local project access for one Fresh application.

    The source app remains authoritative. This table lets Fresh Key show an
    accurate union of people across connected apps without treating one app's
    local role as a global role or removing a person who still collaborates in
    another app.
    """
    __tablename__ = "fresh_key_project_app_members"
    __table_args__ = (UniqueConstraint("project_id", "identity_id", "app_code", name="uq_fk_project_app_member"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_projects.id", ondelete="CASCADE"), index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    app_code: Mapped[str] = mapped_column(String(80), index=True)
    local_role: Mapped[str] = mapped_column(String(40), default="member")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyProjectBinding(Base):
    __tablename__ = "fresh_key_project_bindings"
    __table_args__ = (UniqueConstraint("app_code", "external_project_id", name="uq_fk_project_binding_app_project"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_projects.id", ondelete="CASCADE"), index=True)
    app_code: Mapped[str] = mapped_column(String(80), index=True)
    external_project_id: Mapped[str] = mapped_column(String(120), index=True)
    external_project_name: Mapped[str] = mapped_column(String(190), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FreshKeyLocalProject(Base):
    """Project record reported by a Fresh application before/after it is linked.

    This is inventory only: work data stays in the source application. A row is
    linked to a Fresh Key connection through FreshKeyProjectBinding.
    """
    __tablename__ = "fresh_key_local_projects"
    __table_args__ = (UniqueConstraint("app_code", "external_project_id", name="uq_fk_local_project_app_project"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_code: Mapped[str] = mapped_column(String(80), index=True)
    external_project_id: Mapped[str] = mapped_column(String(120), index=True)
    external_project_name: Mapped[str] = mapped_column(String(190), default="")
    website_url: Mapped[str] = mapped_column(String(500), default="")
    organization_name: Mapped[str] = mapped_column(String(190), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FreshKeyLocalProjectAccess(Base):
    __tablename__ = "fresh_key_local_project_access"
    __table_args__ = (UniqueConstraint("local_project_id", "identity_id", name="uq_fk_local_project_identity"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_local_projects.id", ondelete="CASCADE"), index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FreshKeyLinkRequest(Base):
    """Pending request to attach/create one app project under an existing Fresh Key link.

    The target application remains the authority that accepts/declines the request
    and, for create_new, creates the local work project before confirming it.
    """
    __tablename__ = "fresh_key_link_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_projects.id", ondelete="CASCADE"), index=True)
    requested_by_identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    target_identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    target_app_code: Mapped[str] = mapped_column(String(80), index=True)
    request_type: Mapped[str] = mapped_column(String(30), default="attach_existing", index=True)
    target_local_project_id: Mapped[str | None] = mapped_column(ForeignKey("fresh_key_local_projects.id", ondelete="SET NULL"), nullable=True, index=True)
    requested_project_name: Mapped[str] = mapped_column(String(190), default="")
    requested_project_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    result_external_project_id: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responder_identity_id: Mapped[str | None] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="SET NULL"), nullable=True, index=True)


class FreshKeyOAuthCredential(Base):
    __tablename__ = "fresh_key_oauth_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(String(40), index=True)
    authenticated_by_identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    provider_subject: Mapped[str] = mapped_column(String(255), default="", index=True)
    provider_name: Mapped[str] = mapped_column(String(190), default="")
    access_token_encrypted: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="")
    refresh_token_encrypted: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="{}")
    status: Mapped[str] = mapped_column(String(30), default="connected", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyAsset(Base):
    __tablename__ = "fresh_key_assets"
    __table_args__ = (UniqueConstraint("provider", "asset_type", "external_id", name="uq_fk_asset_provider_type_external"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(String(40), index=True)
    asset_type: Mapped[str] = mapped_column(String(60), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    metadata_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyAssetAuthorization(Base):
    __tablename__ = "fresh_key_asset_authorizations"
    __table_args__ = (UniqueConstraint("credential_id", "asset_id", name="uq_fk_asset_auth_credential_asset"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_oauth_credentials.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_assets.id", ondelete="CASCADE"), index=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="")
    scopes: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyProjectConnection(Base):
    __tablename__ = "fresh_key_project_connections"
    __table_args__ = (UniqueConstraint("project_id", "asset_id", name="uq_fk_project_connection_asset"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_projects.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_assets.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_oauth_credentials.id", ondelete="RESTRICT"), index=True)
    asset_authorization_id: Mapped[int | None] = mapped_column(ForeignKey("fresh_key_asset_authorizations.id", ondelete="SET NULL"), nullable=True)
    connected_by_identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="RESTRICT"), index=True)
    created_via: Mapped[str] = mapped_column(String(80), default="engine")
    capabilities_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="[]")
    status: Mapped[str] = mapped_column(String(30), default="connected", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FreshKeyOAuthTransaction(Base):
    __tablename__ = "fresh_key_oauth_transactions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    app_code: Mapped[str] = mapped_column(String(80), default="engine")
    return_url: Mapped[str] = mapped_column(String(1200), default="")
    status: Mapped[str] = mapped_column(String(30), default="started", index=True)
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    discovered_asset_ids_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), 'mysql'), default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class FreshKeySsoCode(Base):
    __tablename__ = "fresh_key_sso_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("fresh_key_identities.id", ondelete="CASCADE"), index=True)
    app_code: Mapped[str] = mapped_column(String(80), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    binding_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    binding_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    binding_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

