from __future__ import annotations

"""Fresh Key shared identity, project and OAuth broker.

Fresh Key intentionally does NOT store employee work data. Linda/Boban/Eda/etc.
keep their own databases. This module only maintains a shared identity graph,
project membership and project-bound external authorizations.
"""

import base64
import hashlib
import html
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.mailer import send_email
from app.models import (
    AccountEmailVerification,
    CustomerAccount,
    FreshKeyAppBinding,
    FreshKeyAsset,
    FreshKeyAssetAuthorization,
    FreshKeyIdentity,
    FreshKeyIdentityBinding,
    FreshKeyLocalProject,
    FreshKeyLocalProjectAccess,
    FreshKeyLinkRequest,
    FreshKeyOAuthCredential,
    FreshKeyOAuthTransaction,
    FreshKeyProject,
    FreshKeyProjectAppMember,
    FreshKeyProjectBinding,
    FreshKeyProjectConnection,
    FreshKeyProjectMember,
    FreshKeySsoCode,
)

bp = Blueprint("fresh_key", __name__)
settings = get_settings()
APP_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
SUPPORTED_PROVIDERS = {"meta", "linkedin", "google_business"}
APP_LABELS = {"linda": "Linda", "boban": "Boban", "webbooster": "Boban", "eda": "Eda", "karel": "Karel", "ema": "Ema", "viki": "Viki", "ai-engine": "Fresh AI"}
ROLE_LABELS = {"owner": "Vlastník", "manager": "Správce", "editor": "Editor", "viewer": "Čtenář"}
LEGACY_ROLE_ALIASES = {"member": "editor", "client": "editor", "agency": "editor", "visitor": "viewer", "reader": "viewer"}
APP_URLS = {
    "linda": os.getenv("FRESH_KEY_LINDA_URL", "https://socialmedia.freshcrackers.cz").rstrip("/"),
    "boban": os.getenv("FRESH_KEY_BOBAN_URL", "https://webbooster.freshcrackers.cz").rstrip("/"),
    "webbooster": os.getenv("FRESH_KEY_BOBAN_URL", "https://webbooster.freshcrackers.cz").rstrip("/"),
    "eda": os.getenv("FRESH_KEY_EDA_URL", "https://finance.freshcrackers.cz").rstrip("/"),
    "karel": os.getenv("FRESH_KEY_KAREL_URL", "https://ai-coder.freshcrackers.cz").rstrip("/"),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()[:190]


def normalize_access_role(value: str, *, allow_owner: bool = True) -> str:
    """Normalize legacy app roles to the shared four-level access model.

    Relationship labels such as client/agency/member are intentionally not
    permissions. Older apps may still send them; for backwards compatibility
    they map to Editor, while viewer/visitor remain read-only.
    """
    role = str(value or "editor").strip().lower()[:40]
    role = LEGACY_ROLE_ALIASES.get(role, role)
    allowed = {"owner", "manager", "editor", "viewer"}
    if role not in allowed:
        role = "editor"
    if role == "owner" and not allow_owner:
        role = "manager"
    return role


def _json(raw, default):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _fernet() -> Fernet:
    raw = (settings.fresh_key_encryption_key or "").strip()
    if len(raw) < 24:
        raise RuntimeError("FRESH_KEY_ENCRYPTION_KEY není nakonfigurován (min. 24 znaků).")
    derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_secret(value: str | None) -> str:
    value = value or ""
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    value = value or ""
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Fresh Key nedokázal dešifrovat uložený token.") from exc


def _provider_configured(provider: str) -> bool:
    if not settings.fresh_key_encryption_key:
        return False
    if provider == "meta":
        return bool(settings.fresh_key_meta_client_id and settings.fresh_key_meta_client_secret)
    if provider == "linkedin":
        return bool(settings.fresh_key_linkedin_client_id and settings.fresh_key_linkedin_client_secret)
    if provider == "google_business":
        return bool(settings.fresh_key_google_client_id and settings.fresh_key_google_client_secret)
    return False


def provider_status() -> dict[str, dict]:
    return {
        "meta": {"label": "Meta", "configured": _provider_configured("meta"), "covers": "Facebook + Instagram"},
        "linkedin": {"label": "LinkedIn", "configured": _provider_configured("linkedin"), "covers": "profil / Page dle oprávnění aplikace"},
        "google_business": {"label": "Google Business", "configured": _provider_configured("google_business"), "covers": "Business Profile"},
    }


def _app_code(payload: dict | None = None) -> str:
    # For authenticated Fresh Key API calls, the API-key guard binds a connector
    # key to a stable app_code on first use. Payload/header values can therefore
    # no longer be used by one Fresh app to impersonate another app namespace.
    bound = str(getattr(g, "fresh_key_app_code", "") or "").strip().lower()
    if bound and APP_CODE_RE.fullmatch(bound):
        return bound
    payload = payload or {}
    candidate = str(payload.get("app_code") or request.headers.get("X-Fresh-App") or "fresh-app").strip().lower()
    return candidate if APP_CODE_RE.fullmatch(candidate) else "fresh-app"




def _identity_for_app_request(db, body: dict | None = None) -> FreshKeyIdentity:
    """Resolve a global identity from the authenticated app + its local user id.

    The caller never gets to authorize itself by presenting an arbitrary global
    identity UUID. The Engine API key authenticates the Fresh application and
    the local user id is resolved through the app-specific identity binding.
    """
    body = body or {}
    app_code = _app_code(body)
    external_user_id = str(
        body.get("external_user_id")
        or request.headers.get("X-Fresh-User")
        or request.args.get("external_user_id")
        or ""
    ).strip()[:120]
    if not external_user_id:
        raise PermissionError("Fresh Key požadavek neobsahuje identitu uživatele aplikace.")
    binding = db.scalar(select(FreshKeyIdentityBinding).where(
        FreshKeyIdentityBinding.app_code == app_code,
        FreshKeyIdentityBinding.external_user_id == external_user_id,
    ))
    if not binding:
        raise PermissionError("Fresh Key identita této aplikace ještě není synchronizována.")
    identity = db.get(FreshKeyIdentity, binding.identity_id)
    if not identity or not identity.active:
        raise PermissionError("Fresh Key identita není aktivní.")
    return identity

def _verified_account(db, account: CustomerAccount) -> bool:
    row = db.get(AccountEmailVerification, account.id)
    return bool(row and row.verified_at)


def _quarantine_unverified_identity(db, identity: FreshKeyIdentity) -> None:
    # An unverified app may have claimed somebody else's e-mail. Never let a
    # later verified central account inherit that app binding. Pending invitations
    # are moved separately; app bindings and self-created projects stay isolated.
    identity.email = f"unverified-{identity.id}@freshkey.invalid"[:190]


def ensure_identity_for_customer(db, account: CustomerAccount) -> FreshKeyIdentity:
    if not _verified_account(db, account):
        raise PermissionError("Fresh Key vyžaduje ověřený e-mail centrálního účtu.")
    email = _norm_email(account.email)
    identity = db.scalar(select(FreshKeyIdentity).where(FreshKeyIdentity.engine_account_id == account.id))
    if identity:
        identity.email_verified = True
        if email and identity.email != email:
            collision = db.scalar(select(FreshKeyIdentity).where(FreshKeyIdentity.email == email, FreshKeyIdentity.id != identity.id))
            if collision and not collision.email_verified:
                # Preserve pending invitations, not unverified app bindings.
                pending = db.scalars(select(FreshKeyProjectMember).where(
                    FreshKeyProjectMember.identity_id == collision.id,
                    FreshKeyProjectMember.status == "pending",
                )).all()
                _quarantine_unverified_identity(db, collision)
                for m in pending:
                    existing = db.scalar(select(FreshKeyProjectMember).where(
                        FreshKeyProjectMember.project_id == m.project_id,
                        FreshKeyProjectMember.identity_id == identity.id,
                    ))
                    if not existing:
                        m.identity_id = identity.id
                        m.status = "active"
                    else:
                        db.delete(m)
            elif not collision:
                identity.email = email
        if account.company_name and not identity.display_name:
            identity.display_name = account.company_name[:190]
        db.commit()
        return identity

    collision = db.scalar(select(FreshKeyIdentity).where(FreshKeyIdentity.email == email)) if email else None
    if collision and collision.email_verified:
        if collision.engine_account_id and collision.engine_account_id != account.id:
            raise RuntimeError("Fresh Key identita je již propojena s jiným ověřeným účtem.")
        identity = collision
    elif collision:
        # Safe placeholder from an invitation can be promoted only if it has no
        # app binding. Otherwise quarantine it and create a clean verified identity.
        bindings = db.scalars(select(FreshKeyIdentityBinding).where(FreshKeyIdentityBinding.identity_id == collision.id)).all()
        pending = db.scalars(select(FreshKeyProjectMember).where(
            FreshKeyProjectMember.identity_id == collision.id,
            FreshKeyProjectMember.status == "pending",
        )).all()
        if not bindings:
            identity = collision
        else:
            _quarantine_unverified_identity(db, collision)
            identity = FreshKeyIdentity(email=email, display_name=(account.company_name or email)[:190], email_verified=True)
            db.add(identity); db.flush()
            for m in pending:
                existing = db.scalar(select(FreshKeyProjectMember).where(
                    FreshKeyProjectMember.project_id == m.project_id,
                    FreshKeyProjectMember.identity_id == identity.id,
                ))
                if not existing:
                    m.identity_id = identity.id
                    m.status = "active"
                else:
                    db.delete(m)
    else:
        identity = FreshKeyIdentity(email=email, display_name=(account.company_name or email)[:190], email_verified=True)
        db.add(identity); db.flush()

    identity.engine_account_id = account.id
    identity.email_verified = True
    if not identity.display_name:
        identity.display_name = (account.company_name or email)[:190]
    # Any e-mail invitation placeholder becomes active only after central proof.
    for m in db.scalars(select(FreshKeyProjectMember).where(
        FreshKeyProjectMember.identity_id == identity.id,
        FreshKeyProjectMember.status == "pending",
    )).all():
        m.status = "active"
    db.flush()
    binding = db.scalar(select(FreshKeyIdentityBinding).where(
        FreshKeyIdentityBinding.app_code == "ai-engine",
        FreshKeyIdentityBinding.external_user_id == account.id,
    ))
    if not binding:
        db.add(FreshKeyIdentityBinding(identity_id=identity.id, app_code="ai-engine", external_user_id=account.id))
    else:
        binding.identity_id = identity.id
    db.commit()
    return identity

def sync_external_identity(db, app_code: str, external_user_id: str, email: str, display_name: str = "", email_verified: bool = False) -> FreshKeyIdentity:
    app_code = app_code[:80]
    external_user_id = str(external_user_id or "").strip()[:120]
    email = _norm_email(email)
    if not external_user_id or not email or "@" not in email:
        raise ValueError("Fresh Key vyžaduje external_user_id a platný e-mail.")
    binding = db.scalar(select(FreshKeyIdentityBinding).where(
        FreshKeyIdentityBinding.app_code == app_code,
        FreshKeyIdentityBinding.external_user_id == external_user_id,
    ))
    if binding:
        identity = db.get(FreshKeyIdentity, binding.identity_id)
        if not identity:
            raise RuntimeError("Poškozená Fresh Key identity binding.")
        if email_verified:
            identity.email_verified = True
        if display_name:
            identity.display_name = display_name[:190]
        binding.last_seen_at = utcnow()
        db.commit()
        return identity

    existing = db.scalar(select(FreshKeyIdentity).where(FreshKeyIdentity.email == email))
    if existing:
        if email_verified and existing.email_verified:
            identity = existing
        elif email_verified and not existing.email_verified:
            old_bindings = db.scalars(select(FreshKeyIdentityBinding).where(FreshKeyIdentityBinding.identity_id == existing.id)).all()
            if old_bindings:
                _quarantine_unverified_identity(db, existing)
                identity = FreshKeyIdentity(email=email, display_name=(display_name or email)[:190], email_verified=True)
                db.add(identity); db.flush()
            else:
                identity = existing
                identity.email_verified = True
        else:
            raise PermissionError("Tento e-mail už Fresh Key zná. Propoj účet přes centrální Fresh Key přihlášení.")
    else:
        identity = FreshKeyIdentity(email=email, display_name=(display_name or email)[:190], email_verified=bool(email_verified))
        db.add(identity); db.flush()
    db.add(FreshKeyIdentityBinding(identity_id=identity.id, app_code=app_code, external_user_id=external_user_id))
    db.commit()
    return identity

def bootstrap_engine_account(db, *, app_code: str, external_user_id: str, email: str, display_name: str = "", email_verified: bool = True) -> dict:
    """Create the free AI Engine shell/Fresh Key identity from any trusted Fresh app.

    No credits are created or required. The account exists so the user can later
    open the central Fresh Key UI. On the first Fresh Account login the normal
    WordPress identity is attached to this pre-provisioned shell.
    """
    if not email_verified:
        raise PermissionError("Pro automatickou aktivaci Fresh Key musí aplikace potvrdit e-mail přihlášeného uživatele.")
    identity = sync_external_identity(
        db, app_code, external_user_id, email, display_name=display_name, email_verified=True
    )
    account = db.get(CustomerAccount, identity.engine_account_id) if identity.engine_account_id else None
    created = False
    if not account:
        account = db.scalar(select(CustomerAccount).where(CustomerAccount.email == _norm_email(email)))
        if account and not account.active:
            raise PermissionError("Účet Fresh AI se stejným e-mailem není aktivní.")
        if not account:
            account = CustomerAccount(
                email=_norm_email(email),
                password_hash="external:fresh-key-bootstrap:" + secrets.token_urlsafe(32),
                company_name="", billing_name=(display_name or "")[:190],
            )
            db.add(account); db.flush(); created = True
        identity.engine_account_id = account.id
        identity.email_verified = True
        verification = db.get(AccountEmailVerification, account.id)
        now = utcnow()
        if not verification:
            db.add(AccountEmailVerification(
                account_id=account.id, token_hash="fresh-key-" + secrets.token_hex(20),
                expires_at=now, sent_at=now, verified_at=now,
            ))
        elif verification.verified_at is None:
            verification.verified_at = now
        binding = db.scalar(select(FreshKeyIdentityBinding).where(
            FreshKeyIdentityBinding.app_code == "ai-engine",
            FreshKeyIdentityBinding.external_user_id == account.id,
        ))
        if not binding:
            db.add(FreshKeyIdentityBinding(identity_id=identity.id, app_code="ai-engine", external_user_id=account.id))
        else:
            binding.identity_id = identity.id
        db.commit()
    base = settings.fresh_key_public_origin.rstrip("/")
    return {
        "identity_id": identity.id,
        "account_id": account.id if account else "",
        "account_created": created,
        "manage_url": base + "/account/fresh-key",
        "login_url": base + "/account/login?next=/account/fresh-key",
        "free": True,
        "credits_required": False,
    }


def _membership(db, identity_id: str, project_id: str) -> FreshKeyProjectMember | None:
    return db.scalar(select(FreshKeyProjectMember).where(
        FreshKeyProjectMember.identity_id == identity_id,
        FreshKeyProjectMember.project_id == project_id,
        FreshKeyProjectMember.status == "active",
    ))


def _require_member(db, identity_id: str, project_id: str, manage: bool = False) -> FreshKeyProjectMember:
    member = _membership(db, identity_id, project_id)
    if not member:
        # Repair only the unambiguous case: the canonical link owner. Older builds
        # could create the link and then fail before the owner membership became
        # query-visible because the SQLAlchemy session has autoflush disabled.
        project = db.get(FreshKeyProject, project_id)
        if project and project.active and project.owner_identity_id == identity_id:
            member = FreshKeyProjectMember(
                project_id=project_id, identity_id=identity_id, role="owner",
                status="active", can_manage_connections=True,
            )
            db.add(member); db.flush()
    if not member or (manage and not member.can_manage_connections):
        raise PermissionError("K tomuto Fresh Key propojení nemáš potřebné oprávnění.")
    return member


def _project_url_key(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
    except Exception:
        return ""
    host = (parts.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return host + path if host else ""


def _project_name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _local_project_binding(db, local: FreshKeyLocalProject) -> FreshKeyProjectBinding | None:
    return db.scalar(select(FreshKeyProjectBinding).where(
        FreshKeyProjectBinding.app_code == local.app_code,
        FreshKeyProjectBinding.external_project_id == local.external_project_id,
    ))


def sync_local_project(db, *, identity_id: str, app_code: str, external_project_id: str, name: str,
                       website_url: str = "", organization_name: str = "", role: str = "member") -> FreshKeyLocalProject:
    """Register a local app project without creating a new project in AI Engine.

    The row is only an inventory/reference record. Work data remains in the source
    application. A Fresh Key link is represented separately by FreshKeyProject +
    FreshKeyProjectBinding for backwards-compatible storage.
    """
    app_code = str(app_code or "").strip().lower()[:80]
    external_project_id = str(external_project_id or "").strip()[:120]
    name = str(name or "").strip()[:190]
    if not APP_CODE_RE.fullmatch(app_code):
        raise ValueError("Neplatný kód Fresh aplikace.")
    if not external_project_id or not name:
        raise ValueError("Pro propojení chybí local project id nebo název projektu.")
    local = db.scalar(select(FreshKeyLocalProject).where(
        FreshKeyLocalProject.app_code == app_code,
        FreshKeyLocalProject.external_project_id == external_project_id,
    ))
    if not local:
        local = FreshKeyLocalProject(
            app_code=app_code,
            external_project_id=external_project_id,
            external_project_name=name,
            website_url=(website_url or "")[:500],
            organization_name=(organization_name or "")[:190],
            active=True,
        )
        db.add(local); db.flush()
    else:
        local.external_project_name = name
        if website_url:
            local.website_url = website_url[:500]
        if organization_name:
            local.organization_name = organization_name[:190]
        local.active = True
        local.last_seen_at = utcnow()
    access = db.scalar(select(FreshKeyLocalProjectAccess).where(
        FreshKeyLocalProjectAccess.local_project_id == local.id,
        FreshKeyLocalProjectAccess.identity_id == identity_id,
    ))
    if not access:
        db.add(FreshKeyLocalProjectAccess(
            local_project_id=local.id,
            identity_id=identity_id,
            role=normalize_access_role(role),
        ))
    else:
        access.role = normalize_access_role(role or access.role)
        access.last_seen_at = utcnow()
    db.flush()
    return local


def list_local_projects(db, identity_id: str, *, unlinked_only: bool = False) -> list[dict]:
    rows = db.execute(
        select(FreshKeyLocalProject, FreshKeyLocalProjectAccess)
        .join(FreshKeyLocalProjectAccess, FreshKeyLocalProjectAccess.local_project_id == FreshKeyLocalProject.id)
        .where(
            FreshKeyLocalProjectAccess.identity_id == identity_id,
            FreshKeyLocalProject.active.is_(True),
        )
        .order_by(FreshKeyLocalProject.external_project_name, FreshKeyLocalProject.app_code)
    ).all()
    out = []
    for local, access in rows:
        binding = _local_project_binding(db, local)
        if unlinked_only and binding:
            continue
        link = db.get(FreshKeyProject, binding.project_id) if binding else None
        out.append({
            "id": local.id,
            "app_code": local.app_code,
            "external_project_id": local.external_project_id,
            "name": local.external_project_name,
            "website_url": local.website_url,
            "organization_name": local.organization_name,
            "role": access.role,
            "linked": bool(binding and link and link.active),
            "fresh_key_id": link.id if link and link.active else "",
            "link_name": link.name if link and link.active else "",
        })
    return out


def _require_local_access(db, identity_id: str, local_project_id: str) -> FreshKeyLocalProject:
    row = db.execute(
        select(FreshKeyLocalProject, FreshKeyLocalProjectAccess)
        .join(FreshKeyLocalProjectAccess, FreshKeyLocalProjectAccess.local_project_id == FreshKeyLocalProject.id)
        .where(
            FreshKeyLocalProject.id == local_project_id,
            FreshKeyLocalProjectAccess.identity_id == identity_id,
            FreshKeyLocalProject.active.is_(True),
        )
    ).first()
    if not row:
        raise PermissionError("K vybranému lokálnímu projektu nemáš Fresh Key přístup.")
    return row[0]


def _upsert_project_app_member(db, *, project_id: str, identity_id: str, app_code: str, local_role: str = "member", status: str = "active") -> FreshKeyProjectAppMember | None:
    app_code = str(app_code or "").strip().lower()[:80]
    if app_code == "webbooster":
        app_code = "boban"
    if not app_code or app_code == "ai-engine" or not APP_CODE_RE.fullmatch(app_code):
        return None
    row = db.scalar(select(FreshKeyProjectAppMember).where(
        FreshKeyProjectAppMember.project_id == project_id,
        FreshKeyProjectAppMember.identity_id == identity_id,
        FreshKeyProjectAppMember.app_code == app_code,
    ))
    if not row:
        row = FreshKeyProjectAppMember(
            project_id=project_id, identity_id=identity_id, app_code=app_code,
            local_role=normalize_access_role(local_role), status=str(status or "active")[:20],
        )
        db.add(row)
    else:
        row.local_role = normalize_access_role(local_role or row.local_role)
        row.status = str(status or row.status or "active")[:20]
        row.updated_at = utcnow()
    return row


def _project_app_members(db, *, project_id: str, identity_id: str) -> list[FreshKeyProjectAppMember]:
    return db.scalars(select(FreshKeyProjectAppMember).where(
        FreshKeyProjectAppMember.project_id == project_id,
        FreshKeyProjectAppMember.identity_id == identity_id,
        FreshKeyProjectAppMember.status == "active",
    ).order_by(FreshKeyProjectAppMember.app_code)).all()


def _recompute_global_member_from_apps(db, member: FreshKeyProjectMember) -> None:
    project = db.get(FreshKeyProject, member.project_id)
    if project and project.owner_identity_id == member.identity_id:
        member.role = "owner"
        member.status = "active"
        member.can_manage_connections = True
        return
    mirrors = _project_app_members(db, project_id=member.project_id, identity_id=member.identity_id)
    if not mirrors:
        return
    roles = {normalize_access_role(x.local_role, allow_owner=False) for x in mirrors}
    if "manager" in roles:
        member.role = "manager"
        member.can_manage_connections = True
    elif "editor" in roles:
        member.role = "editor"
        member.can_manage_connections = False
    else:
        member.role = "viewer"
        member.can_manage_connections = False
    member.status = "active"


def _attach_local_to_link(db, *, identity_id: str, link: FreshKeyProject, local: FreshKeyLocalProject) -> None:
    _require_member(db, identity_id, link.id)
    existing = _local_project_binding(db, local)
    # Fresh Key represents one common project. One link may therefore contain at
    # most one local project from each Fresh app. This prevents accidental states
    # such as two different Linda projects being hidden under one connection.
    same_app = db.scalar(select(FreshKeyProjectBinding).where(
        FreshKeyProjectBinding.project_id == link.id,
        FreshKeyProjectBinding.app_code == local.app_code,
    ))
    if same_app and same_app.external_project_id != local.external_project_id:
        label = APP_LABELS.get(local.app_code, local.app_code.title())
        raise ValueError(f"V tomto Propojení už je jiný projekt z aplikace {label}. Nejprve ho odpoj nebo použij jiné Propojení.")
    if existing:
        if existing.project_id != link.id:
            raise ValueError("Vybraný lokální projekt už patří do jiného propojení.")
        existing.external_project_name = local.external_project_name
        existing.last_seen_at = utcnow()
        access = db.scalar(select(FreshKeyLocalProjectAccess).where(
            FreshKeyLocalProjectAccess.local_project_id == local.id,
            FreshKeyLocalProjectAccess.identity_id == identity_id,
        ))
        _upsert_project_app_member(
            db, project_id=link.id, identity_id=identity_id, app_code=local.app_code,
            local_role=(access.role if access else ("owner" if identity_id == link.owner_identity_id else "member")),
            status="active",
        )
        return
    db.add(FreshKeyProjectBinding(
        project_id=link.id,
        app_code=local.app_code,
        external_project_id=local.external_project_id,
        external_project_name=local.external_project_name,
    ))
    access = db.scalar(select(FreshKeyLocalProjectAccess).where(
        FreshKeyLocalProjectAccess.local_project_id == local.id,
        FreshKeyLocalProjectAccess.identity_id == identity_id,
    ))
    _upsert_project_app_member(
        db, project_id=link.id, identity_id=identity_id, app_code=local.app_code,
        local_role=(access.role if access else ("owner" if identity_id == link.owner_identity_id else "member")),
        status="active",
    )


def create_or_extend_link(db, *, identity_id: str, local_project_ids: list[str], label: str = "",
                          target_link_id: str = "") -> FreshKeyProject:
    ids = [str(x or "").strip() for x in local_project_ids if str(x or "").strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ValueError("Vyber alespoň jeden existující projekt k propojení.")
    locals_ = [_require_local_access(db, identity_id, x) for x in ids]
    bound_ids = set()
    for local in locals_:
        binding = _local_project_binding(db, local)
        if binding:
            bound_ids.add(binding.project_id)
    if target_link_id:
        link = db.get(FreshKeyProject, target_link_id)
        if not link or not link.active:
            raise ValueError("Vybrané propojení neexistuje.")
        _require_member(db, identity_id, link.id)
        if any(x != link.id for x in bound_ids):
            raise ValueError("Některý z projektů už patří do jiného propojení.")
    elif bound_ids:
        if len(bound_ids) != 1:
            raise ValueError("Vybrané projekty už patří do více různých propojení.")
        link = db.get(FreshKeyProject, next(iter(bound_ids)))
        if not link or not link.active:
            raise ValueError("Existující propojení už není aktivní.")
        _require_member(db, identity_id, link.id)
    else:
        first = locals_[0]
        link = FreshKeyProject(
            name=(label.strip() or first.external_project_name)[:190],
            website_url=first.website_url[:500],
            organization_name=first.organization_name[:190],
            owner_identity_id=identity_id,
        )
        db.add(link); db.flush()
        db.add(FreshKeyProjectMember(
            project_id=link.id,
            identity_id=identity_id,
            role="owner",
            status="active",
            can_manage_connections=True,
        ))
        # SessionLocal uses autoflush=False. The member row must be flushed before
        # _attach_local_to_link() calls _require_member(); otherwise a brand-new
        # one-app link exists only in the pending unit of work and the membership
        # SELECT returns nothing, producing a false 403 for the actual owner.
        db.flush()
    if label.strip():
        link.name = label.strip()[:190]
    if not link.website_url:
        link.website_url = next((x.website_url for x in locals_ if x.website_url), "")[:500]
    if not link.organization_name:
        link.organization_name = next((x.organization_name for x in locals_ if x.organization_name), "")[:190]
    for local in locals_:
        _attach_local_to_link(db, identity_id=identity_id, link=link, local=local)
    db.commit()
    return link


def _matching_links_for_local(db, identity_id: str, local: FreshKeyLocalProject) -> list[FreshKeyProject]:
    accessible = list_identity_projects(db, identity_id)
    url_key = _project_url_key(local.website_url)
    name_key = _project_name_key(local.external_project_name)
    strong, weak = [], []
    for item in accessible:
        link = db.get(FreshKeyProject, item["id"])
        if not link:
            continue
        if url_key and _project_url_key(link.website_url) == url_key:
            strong.append(link); continue
        if name_key and _project_name_key(link.name) == name_key:
            weak.append(link)
    return strong or weak



def _link_bindings(db, project_id: str) -> list[FreshKeyProjectBinding]:
    return db.scalars(select(FreshKeyProjectBinding).where(
        FreshKeyProjectBinding.project_id == project_id
    ).order_by(FreshKeyProjectBinding.app_code)).all()


def _link_connection_count(db, project_id: str) -> int:
    return len(db.scalars(select(FreshKeyProjectConnection).where(
        FreshKeyProjectConnection.project_id == project_id,
        FreshKeyProjectConnection.status != "deleted",
    )).all())


def detach_local_from_link(db, *, identity_id: str, project_id: str, app_code: str, external_project_id: str) -> dict:
    """Remove only the Fresh Key binding. Never delete the local work project."""
    link = db.get(FreshKeyProject, project_id)
    if not link or not link.active:
        raise ValueError("Propojení neexistuje.")
    _require_member(db, identity_id, project_id, manage=True)
    app_code = str(app_code or "").strip().lower()
    binding = db.scalar(select(FreshKeyProjectBinding).where(
        FreshKeyProjectBinding.project_id == project_id,
        FreshKeyProjectBinding.app_code == app_code,
        FreshKeyProjectBinding.external_project_id == str(external_project_id or "").strip(),
    ))
    if not binding:
        raise ValueError("Vybraná aplikace už v tomto Propojení není.")
    bindings = _link_bindings(db, project_id)
    if len(bindings) == 1 and _link_connection_count(db, project_id):
        raise ValueError("Poslední aplikaci nelze odpojit, dokud má Propojení aktivní externí konektory.")
    db.delete(binding)
    mirror = db.scalar(select(FreshKeyProjectAppMember).where(
        FreshKeyProjectAppMember.project_id == project_id,
        FreshKeyProjectAppMember.identity_id == identity_id,
        FreshKeyProjectAppMember.app_code == app_code,
    ))
    if mirror:
        db.delete(mirror)
    if len(bindings) == 1:
        link.active = False
        pending = db.scalars(select(FreshKeyLinkRequest).where(
            FreshKeyLinkRequest.project_id == project_id,
            FreshKeyLinkRequest.status == "pending",
        )).all()
        for req in pending:
            req.status = "expired"
            req.responded_at = utcnow()
    db.commit()
    return {"project_id": project_id, "archived": not bool(link.active)}


def merge_links(db, *, identity_id: str, target_link_id: str, source_link_id: str) -> FreshKeyProject:
    """Merge two Fresh Key links without touching work data in source apps.

    This is deliberately a Fresh Key operation: both app projects were already
    published by their authoritative apps. The merge only says they represent
    the same common project.
    """
    if target_link_id == source_link_id:
        raise ValueError("Vyber dvě různá Propojení.")
    target = db.get(FreshKeyProject, target_link_id)
    source = db.get(FreshKeyProject, source_link_id)
    if not target or not target.active or not source or not source.active:
        raise ValueError("Jedno z Propojení už není aktivní.")
    _require_member(db, identity_id, target.id, manage=True)
    _require_member(db, identity_id, source.id, manage=True)

    target_by_app = {b.app_code: b for b in _link_bindings(db, target.id)}
    source_bindings = _link_bindings(db, source.id)
    conflicts = []
    for b in source_bindings:
        other = target_by_app.get(b.app_code)
        if other and other.external_project_id != b.external_project_id:
            conflicts.append(APP_LABELS.get(b.app_code, b.app_code.title()))
    if conflicts:
        raise ValueError("Propojení nelze sloučit: obě obsahují jiný projekt z aplikace " + ", ".join(sorted(set(conflicts))) + ".")

    # Move local app bindings first. Same binding cannot normally exist twice
    # because (app_code, external_project_id) is globally unique; handle it safely.
    for b in source_bindings:
        other = target_by_app.get(b.app_code)
        if other and other.external_project_id == b.external_project_id:
            db.delete(b)
        else:
            b.project_id = target.id
            target_by_app[b.app_code] = b

    # Merge read-only per-app membership mirrors.
    rank = {"viewer": 1, "editor": 2, "manager": 3, "owner": 4}
    source_app_members = db.scalars(select(FreshKeyProjectAppMember).where(
        FreshKeyProjectAppMember.project_id == source.id
    )).all()
    for row in source_app_members:
        existing = db.scalar(select(FreshKeyProjectAppMember).where(
            FreshKeyProjectAppMember.project_id == target.id,
            FreshKeyProjectAppMember.identity_id == row.identity_id,
            FreshKeyProjectAppMember.app_code == row.app_code,
        ))
        if existing:
            if rank.get(normalize_access_role(row.local_role), 0) > rank.get(normalize_access_role(existing.local_role), 0):
                existing.local_role = row.local_role
            existing.status = "active" if "active" in {existing.status, row.status} else existing.status
            db.delete(row)
        else:
            row.project_id = target.id

    # Merge the people overview, keeping the target owner as the canonical owner.
    source_members = db.scalars(select(FreshKeyProjectMember).where(
        FreshKeyProjectMember.project_id == source.id
    )).all()
    for member in source_members:
        existing = db.scalar(select(FreshKeyProjectMember).where(
            FreshKeyProjectMember.project_id == target.id,
            FreshKeyProjectMember.identity_id == member.identity_id,
        ))
        if existing:
            existing.can_manage_connections = bool(existing.can_manage_connections or member.can_manage_connections)
            if existing.role != "owner" and member.role in {"owner", "manager"}:
                existing.role = "manager"
            existing.status = "active" if "active" in {existing.status, member.status} else existing.status
            db.delete(member)
        else:
            member.project_id = target.id
            if member.role == "owner" and member.identity_id != target.owner_identity_id:
                member.role = "manager"

    # Connections are shared by the common project; dedupe by asset.
    for conn in db.scalars(select(FreshKeyProjectConnection).where(
        FreshKeyProjectConnection.project_id == source.id
    )).all():
        duplicate = db.scalar(select(FreshKeyProjectConnection).where(
            FreshKeyProjectConnection.project_id == target.id,
            FreshKeyProjectConnection.asset_id == conn.asset_id,
        ))
        if duplicate:
            conn.status = "deleted"
        else:
            conn.project_id = target.id

    # Pending requests follow the surviving link, unless the same target app is
    # already connected after the merge.
    connected_apps = {b.app_code for b in target_by_app.values()}
    for req in db.scalars(select(FreshKeyLinkRequest).where(
        FreshKeyLinkRequest.project_id == source.id
    )).all():
        if req.status == "pending" and req.target_app_code in connected_apps:
            req.status = "expired"
            req.responded_at = utcnow()
        else:
            req.project_id = target.id

    for tx in db.scalars(select(FreshKeyOAuthTransaction).where(
        FreshKeyOAuthTransaction.project_id == source.id,
        FreshKeyOAuthTransaction.status.in_(["started", "assets_ready"]),
    )).all():
        tx.project_id = target.id

    if not target.website_url and source.website_url:
        target.website_url = source.website_url
    if not target.organization_name and source.organization_name:
        target.organization_name = source.organization_name
    source.active = False
    db.commit()
    return target


def merge_suggestions(db, identity_id: str) -> list[dict]:
    """Strong duplicate suggestions only: same normalized website URL."""
    links = list_identity_projects(db, identity_id)
    groups: dict[str, list[dict]] = {}
    for link in links:
        key = _project_url_key(str(link.get("website_url") or ""))
        if key:
            groups.setdefault(key, []).append(link)
    out = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        # Only suggest pairs the current identity can manage on both sides and
        # that do not collide on two different projects from the same app.
        base = items[0]
        for other in items[1:]:
            if not base.get("can_manage_connections") or not other.get("can_manage_connections"):
                continue
            apps_a = {b["app_code"]: b["external_project_id"] for b in base.get("bindings", [])}
            apps_b = {b["app_code"]: b["external_project_id"] for b in other.get("bindings", [])}
            if any(code in apps_a and apps_a[code] != pid for code, pid in apps_b.items()):
                continue
            out.append({"target": base, "source": other, "url_key": key})
    return out


def publish_local_project(db, *, identity_id: str, app_code: str, external_project_id: str, name: str,
                          website_url: str = "", organization_name: str = "", fresh_project_id: str = "") -> dict:
    """Explicit app publish with duplicate prevention.

    If a strong/weak existing Fresh Key candidate is found, no second link is
    created. The caller receives `match_required` and can continue in the AI
    Engine UI. A brand-new link is created only when no candidate exists.
    """
    local = sync_local_project(
        db, identity_id=identity_id, app_code=app_code, external_project_id=external_project_id,
        name=name, website_url=website_url, organization_name=organization_name, role="owner",
    )
    existing = _local_project_binding(db, local)
    if existing:
        link = db.get(FreshKeyProject, existing.project_id)
        if not link or not link.active:
            raise RuntimeError("Poškozené Fresh Key propojení.")
        _require_member(db, identity_id, link.id)
        return {"status": "linked", "link": link, "local": local, "candidates": []}
    if fresh_project_id:
        link = create_or_extend_link(db, identity_id=identity_id, local_project_ids=[local.id], target_link_id=fresh_project_id)
        return {"status": "linked", "link": link, "local": local, "candidates": []}
    candidates = _matching_links_for_local(db, identity_id, local)
    if candidates:
        return {"status": "match_required", "link": None, "local": local, "candidates": candidates}
    link = create_or_extend_link(db, identity_id=identity_id, local_project_ids=[local.id])
    return {"status": "created", "link": link, "local": local, "candidates": []}


def sync_project(db, *, identity_id: str, app_code: str, external_project_id: str, name: str,
                 website_url: str = "", organization_name: str = "", fresh_project_id: str = "") -> FreshKeyProject:
    """Compatibility API: map a local project to one Fresh Key connection.

    v0.7.3 no longer models this as "creating a project in AI Engine". The source
    project is first registered as local inventory. If a target Fresh Key ID is
    supplied, it is attached there. Otherwise an explicit publish from the source
    app creates a one-app Fresh Key link. Other apps are attached only after an
    explicit proposal is confirmed inside the target app; names and URLs never
    auto-merge projects.
    """
    local = sync_local_project(
        db,
        identity_id=identity_id,
        app_code=app_code,
        external_project_id=external_project_id,
        name=name,
        website_url=website_url,
        organization_name=organization_name,
        role="owner",
    )
    existing = _local_project_binding(db, local)
    if existing:
        link = db.get(FreshKeyProject, existing.project_id)
        if not link:
            raise RuntimeError("Poškozené Fresh Key propojení.")
        _require_member(db, identity_id, link.id)
        return link
    if fresh_project_id:
        return create_or_extend_link(
            db,
            identity_id=identity_id,
            local_project_ids=[local.id],
            target_link_id=fresh_project_id,
        )
    # Publishing from a source app intentionally creates a one-app Fresh Key link.
    # Other applications are attached only through an explicit proposal confirmed
    # in the target application; names/URLs are hints, never automatic identity.
    return create_or_extend_link(db, identity_id=identity_id, local_project_ids=[local.id])


def _member_apps(db, project_id: str, identity_id: str) -> list[dict]:
    rows = _project_app_members(db, project_id=project_id, identity_id=identity_id)
    return [{
        "code": str(row.app_code or ""),
        "label": APP_LABELS.get(str(row.app_code or "").lower(), str(row.app_code or "").replace("-", " ").title()),
        "role": normalize_access_role(row.local_role),
        "role_label": ROLE_LABELS.get(normalize_access_role(row.local_role), normalize_access_role(row.local_role)),
    } for row in rows]


def _member_payload(db, member: FreshKeyProjectMember, ident: FreshKeyIdentity) -> dict:
    return {
        "identity_id": ident.id,
        "email": ident.email,
        "display_name": ident.display_name,
        "role": member.role,
        "role_label": ROLE_LABELS.get(member.role, member.role),
        "status": member.status,
        "can_manage_connections": bool(member.can_manage_connections),
        "apps": _member_apps(db, member.project_id, ident.id),
    }


def list_project_members(db, identity_id: str, project_id: str) -> list[dict]:
    _require_member(db, identity_id, project_id)
    rows = db.execute(
        select(FreshKeyProjectMember, FreshKeyIdentity)
        .join(FreshKeyIdentity, FreshKeyIdentity.id == FreshKeyProjectMember.identity_id)
        .where(FreshKeyProjectMember.project_id == project_id)
        .order_by(FreshKeyProjectMember.role, FreshKeyIdentity.email)
    ).all()
    return [_member_payload(db, member, ident) for member, ident in rows]


def _send_project_invitation(db, *, project: FreshKeyProject, email: str, role: str) -> bool:
    login_url = settings.fresh_key_public_origin.rstrip("/") + "/account/login"
    register_url = settings.fresh_key_public_origin.rstrip("/") + "/account/register"
    role_label = ROLE_LABELS.get(role, role)
    text = (
        f"Byl jsi pozván do projektu {project.name} ve Fresh AI jako {role_label}.\n\n"
        f"Pokud už účet máš, přihlas se: {login_url}\n"
        f"Pokud účet ještě nemáš, vytvoř si ho: {register_url}\n\n"
        "Po přihlášení nebo ověření stejného e-mailu se čekající přístup aktivuje automaticky."
    )
    safe_project = html.escape(project.name or "Projekt")
    safe_role = html.escape(role_label)
    html_body = (
        f"<h2>Pozvánka do projektu {safe_project}</h2>"
        f"<p>Byl jsi pozván jako <strong>{safe_role}</strong>.</p>"
        f"<p><a href=\"{login_url}\">Přihlásit se</a> &nbsp;·&nbsp; <a href=\"{register_url}\">Vytvořit účet</a></p>"
        "<p>Po přihlášení nebo ověření stejného e-mailu se čekající přístup aktivuje automaticky.</p>"
    )
    try:
        send_email(db, email, f"Pozvánka do projektu {project.name} · Fresh AI", text, html_body)
        return True
    except Exception:
        return False



def notify_app_project_share(db, *, actor_identity_id: str, recipient_email: str, app_code: str,
                             project_name: str, role: str = "member", action_url: str = "") -> dict:
    """Send a collaboration confirmation/invitation without creating Fresh Key access.

    The source application remains authoritative for the local project membership.
    Fresh Key is used here only as the common Fresh identity/e-mail service.
    """
    email = _norm_email(recipient_email)
    app_code = str(app_code or "").strip().lower()[:80]
    project_name = str(project_name or "Projekt").strip()[:190] or "Projekt"
    role = normalize_access_role(role, allow_owner=False)
    if not email or "@" not in email:
        raise ValueError("Doplň platný e-mail uživatele.")
    if not APP_CODE_RE.fullmatch(app_code):
        raise ValueError("Neplatná Fresh aplikace.")

    actor = db.get(FreshKeyIdentity, actor_identity_id)
    identity = db.scalar(select(FreshKeyIdentity).where(FreshKeyIdentity.email == email))
    account = db.scalar(select(CustomerAccount).where(CustomerAccount.email == email, CustomerAccount.active.is_(True)))
    known = bool((identity and identity.email_verified) or (account and _verified_account(db, account)))
    actor_label = (actor.display_name or actor.email) if actor else "Uživatel Fresh AI"
    app_label = APP_LABELS.get(app_code, app_code.replace("-", " ").title())
    role_label = ROLE_LABELS.get(role, role)
    url = str(action_url or APP_URLS.get(app_code, "") or "")[:1200]

    if known:
        subject = f"Spolupráce na projektu {project_name} · {app_label}"
        text = (
            f"{actor_label} s tebou sdílí projekt {project_name} v aplikaci {app_label} jako {role_label}.\n\n"
            "Přístup a oprávnění k projektu spravuje přímo tato aplikace."
            + ((f"\n\nOtevřít projekt: {url}") if url else "")
        )
        html_body = (
            f"<h2>Spolupráce na projektu {html.escape(project_name)}</h2>"
            f"<p><strong>{html.escape(actor_label)}</strong> s tebou sdílí projekt v aplikaci "
            f"<strong>{html.escape(app_label)}</strong> jako {html.escape(role_label)}.</p>"
            "<p>Přístup a oprávnění spravuje přímo daná aplikace.</p>"
            + (f'<p><a href="{html.escape(url, quote=True)}">Otevřít projekt</a></p>' if url else "")
        )
        recipient_status = "existing"
    else:
        subject = f"Pozvánka k projektu {project_name} · {app_label}"
        text = (
            f"{actor_label} tě zve ke spolupráci na projektu {project_name} v aplikaci {app_label}.\n\n"
            "Přístup se aktivuje po přihlášení nebo vytvoření Fresh účtu se stejným e-mailem."
            + ((f"\n\nPřijmout pozvánku: {url}") if url else "")
        )
        html_body = (
            f"<h2>Pozvánka k projektu {html.escape(project_name)}</h2>"
            f"<p><strong>{html.escape(actor_label)}</strong> tě zve ke spolupráci v aplikaci "
            f"<strong>{html.escape(app_label)}</strong>.</p>"
            "<p>Přístup se aktivuje po přihlášení nebo vytvoření Fresh účtu se stejným e-mailem.</p>"
            + (f'<p><a href="{html.escape(url, quote=True)}">Přijmout pozvánku</a></p>' if url else "")
        )
        recipient_status = "invited"

    sent = False
    try:
        send_email(db, email, subject, text, html_body)
        sent = True
    except Exception:
        sent = False
    return {"recipient_status": recipient_status, "email_sent": sent, "known_fresh_identity": known}


def _send_project_shared_notice(db, *, project: FreshKeyProject, email: str, role: str) -> bool:
    role_label = ROLE_LABELS.get(role, role)
    text = (
        f"Projekt {project.name} s tebou byl nasdílen ve Fresh AI jako {role_label}.\n\n"
        "Projekt otevři v aplikaci, ve které s ním spolupracuješ. Lokální oprávnění vždy řídí daná aplikace."
    )
    html_body = (
        f"<h2>Spolupráce na projektu {html.escape(project.name)}</h2>"
        f"<p>Projekt s tebou byl nasdílen jako <strong>{html.escape(role_label)}</strong>.</p>"
        "<p>Projekt otevři v aplikaci, ve které s ním spolupracuješ. Lokální oprávnění vždy řídí daná aplikace.</p>"
    )
    try:
        send_email(db, email, f"Spolupráce na projektu {project.name} · Fresh AI", text, html_body)
        return True
    except Exception:
        return False

def add_project_member(db, *, actor_identity_id: str, project_id: str, email: str, role: str = "member",
                       can_manage_connections: bool = False, notify: bool = True,
                       source_app_code: str = "") -> FreshKeyProjectMember:
    _require_member(db, actor_identity_id, project_id, manage=True)
    email = _norm_email(email)
    role = normalize_access_role(role, allow_owner=False)
    if not email or "@" not in email:
        raise ValueError("Doplň platný e-mail člena projektu.")
    identity = db.scalar(select(FreshKeyIdentity).where(FreshKeyIdentity.email == email))
    if not identity:
        # A verified Engine/Fresh account can be materialized immediately.
        # Otherwise keep a pending identity until the same e-mail is verified.
        account = db.scalar(select(CustomerAccount).where(
            CustomerAccount.email == email, CustomerAccount.active.is_(True)
        ))
        if account and _verified_account(db, account):
            identity = ensure_identity_for_customer(db, account)
        else:
            identity = FreshKeyIdentity(email=email, display_name=email)
            db.add(identity); db.flush()
    member = db.scalar(select(FreshKeyProjectMember).where(
        FreshKeyProjectMember.identity_id == identity.id, FreshKeyProjectMember.project_id == project_id
    ))
    target_status = "active" if identity.email_verified else "pending"
    was_pending = False
    if not member:
        member = FreshKeyProjectMember(
            project_id=project_id, identity_id=identity.id,
            role=role,
            status=target_status, can_manage_connections=bool(role == "manager"),
        )
        db.add(member)
        was_pending = target_status == "pending"
    else:
        if member.role != "owner":
            # The central role is only an authorization summary. App-specific
            # roles are stored separately in FreshKeyProjectAppMember.
            member.role = role
            member.can_manage_connections = bool(role == "manager")
        if identity.email_verified:
            member.status = "active"
        elif member.status != "active":
            member.status = "pending"
            was_pending = True

    if source_app_code:
        _upsert_project_app_member(
            db, project_id=project_id, identity_id=identity.id, app_code=source_app_code,
            local_role=role, status=target_status,
        )
        if identity.email_verified:
            _recompute_global_member_from_apps(db, member)
    db.commit()
    project = db.get(FreshKeyProject, project_id)
    if project and notify:
        if was_pending:
            _send_project_invitation(db, project=project, email=email, role=role)
        elif identity.email_verified:
            _send_project_shared_notice(db, project=project, email=email, role=role)
    return member


def update_project_member(db, *, actor_identity_id: str, project_id: str, target_identity_id: str,
                          role: str, can_manage_connections: bool = False, source_app_code: str = "") -> FreshKeyProjectMember:
    _require_member(db, actor_identity_id, project_id, manage=True)
    member = db.scalar(select(FreshKeyProjectMember).where(
        FreshKeyProjectMember.project_id == project_id,
        FreshKeyProjectMember.identity_id == target_identity_id,
    ))
    if not member:
        raise ValueError("Člen propojení nebyl nalezen.")
    if member.role == "owner":
        raise ValueError("Roli vlastníka nelze tímto způsobem změnit.")
    role = normalize_access_role(role, allow_owner=False)
    if source_app_code:
        _upsert_project_app_member(
            db, project_id=project_id, identity_id=target_identity_id, app_code=source_app_code,
            local_role=role, status="active",
        )
        _recompute_global_member_from_apps(db, member)
    else:
        member.role = role
        member.can_manage_connections = bool(role == "manager")
    db.commit()
    return member


def remove_project_member(db, *, actor_identity_id: str, project_id: str, target_identity_id: str,
                          source_app_code: str = "") -> None:
    _require_member(db, actor_identity_id, project_id, manage=True)
    member = db.scalar(select(FreshKeyProjectMember).where(
        FreshKeyProjectMember.project_id == project_id,
        FreshKeyProjectMember.identity_id == target_identity_id,
    ))
    if not member:
        return
    if member.role == "owner":
        raise ValueError("Vlastníka propojení nelze odebrat.")
    if source_app_code:
        app_code = str(source_app_code or "").strip().lower()
        if app_code == "webbooster": app_code = "boban"
        mirror = db.scalar(select(FreshKeyProjectAppMember).where(
            FreshKeyProjectAppMember.project_id == project_id,
            FreshKeyProjectAppMember.identity_id == target_identity_id,
            FreshKeyProjectAppMember.app_code == app_code,
        ))
        if mirror:
            db.delete(mirror); db.flush()
        remaining = _project_app_members(db, project_id=project_id, identity_id=target_identity_id)
        if remaining:
            _recompute_global_member_from_apps(db, member)
        else:
            db.delete(member)
    else:
        db.delete(member)
    db.commit()



def list_identity_projects(db, identity_id: str) -> list[dict]:
    rows = db.execute(
        select(FreshKeyProject, FreshKeyProjectMember)
        .join(FreshKeyProjectMember, FreshKeyProjectMember.project_id == FreshKeyProject.id)
        .where(FreshKeyProjectMember.identity_id == identity_id, FreshKeyProjectMember.status == "active", FreshKeyProject.active.is_(True))
        .order_by(FreshKeyProject.name)
    ).all()
    out = []
    for project, member in rows:
        bindings = db.scalars(select(FreshKeyProjectBinding).where(FreshKeyProjectBinding.project_id == project.id)).all()
        out.append({
            "id": project.id, "name": project.name, "website_url": project.website_url,
            "organization_name": project.organization_name, "role": member.role,
            "can_manage_connections": bool(member.can_manage_connections),
            "bindings": [{"app_code": b.app_code, "app_label": APP_LABELS.get(str(b.app_code or "").lower(), str(b.app_code or "").replace("-", " ").title()),
                          "external_project_id": b.external_project_id, "external_project_name": b.external_project_name} for b in bindings],
        })
    return out



def identity_apps(db, identity_id: str, project_id: str = "") -> list[dict]:
    """Return Fresh apps bound to this identity plus local projects known for each app."""
    bindings = db.scalars(select(FreshKeyIdentityBinding).where(
        FreshKeyIdentityBinding.identity_id == identity_id
    )).all()
    connected_codes = set()
    if project_id:
        connected_codes = {
            str(x.app_code or "").strip().lower()
            for x in db.scalars(select(FreshKeyProjectBinding).where(
                FreshKeyProjectBinding.project_id == project_id
            )).all()
        }
    locals_all = list_local_projects(db, identity_id)
    seen = set()
    result = []
    for b in bindings:
        code = str(b.app_code or "").strip().lower()
        if not code or code == "ai-engine" or code in seen:
            continue
        seen.add(code)
        projects = [x for x in locals_all if str(x.get("app_code") or "").lower() == code]
        result.append({
            "code": code,
            "label": APP_LABELS.get(code, code.replace("-", " ").title()),
            "account_connected": True,
            "already_connected": code in connected_codes,
            "projects": projects,
        })
    return sorted(result, key=lambda x: x["label"].lower())


def _app_request_url(app_code: str, request_id: str, local: FreshKeyLocalProject | None = None) -> str:
    base = APP_URLS.get(app_code, "")
    if not base:
        return settings.fresh_key_public_origin.rstrip("/") + "/account/fresh-key"
    if app_code == "linda":
        if local:
            return base + "/?" + urlencode({"page": "project_setup", "project_id": local.external_project_id, "fresh_key_request": request_id})
        return base + "/?" + urlencode({"page": "projects", "fresh_key_request": request_id})
    if app_code in {"boban", "webbooster"}:
        if local:
            return base + f"/projects/{local.external_project_id}/access?fresh_key_request={request_id}"
        return base + f"/dashboard?fresh_key_request={request_id}"
    if app_code == "eda":
        if local:
            return base + f"/projects/{local.external_project_id}?fresh_key_request={request_id}"
        return base + f"/projects?fresh_key_request={request_id}"
    return base


def _send_link_request_email(db, req: FreshKeyLinkRequest) -> bool:
    link = db.get(FreshKeyProject, req.project_id)
    target = db.get(FreshKeyIdentity, req.target_identity_id)
    actor = db.get(FreshKeyIdentity, req.requested_by_identity_id)
    local = db.get(FreshKeyLocalProject, req.target_local_project_id) if req.target_local_project_id else None
    if not link or not target or not target.email:
        return False
    actor_label = (actor.display_name or actor.email) if actor else "Uživatel Fresh AI"
    app_label = APP_LABELS.get(req.target_app_code, req.target_app_code.title())
    action = "propojení projektu" if req.request_type == "attach_existing" else "vytvoření nového propojeného projektu"
    url = _app_request_url(req.target_app_code, req.id, local)
    target_project = local.external_project_name if local else req.requested_project_name
    text = (
        f"{actor_label} navrhuje {action} v aplikaci {app_label}.\n\n"
        f"Fresh Key: {link.name}\n"
        f"Projekt v aplikaci: {target_project or link.name}\n\n"
        f"Před propojením je potřeba potvrzení přímo v aplikaci {app_label}:\n{url}\n"
    )
    html_body = (
        f"<h2>Návrh propojení · {html.escape(app_label)}</h2>"
        f"<p><strong>{html.escape(actor_label)}</strong> navrhuje {html.escape(action)}.</p>"
        f"<p>Fresh Key: <strong>{html.escape(link.name)}</strong><br>Projekt v aplikaci: <strong>{html.escape(target_project or link.name)}</strong></p>"
        f"<p><a href=\"{html.escape(url, quote=True)}\">Otevřít {html.escape(app_label)} a potvrdit</a></p>"
        "<p>Dokud návrh nepotvrdíš v cílové aplikaci, Fresh Key žádné projekty nespojí ani nevytvoří.</p>"
    )
    try:
        send_email(db, target.email, f"Potvrď propojení projektu · {app_label} · Fresh Key", text, html_body)
        return True
    except Exception:
        return False


def _send_link_result_email(db, req: FreshKeyLinkRequest, accepted: bool) -> bool:
    actor = db.get(FreshKeyIdentity, req.requested_by_identity_id)
    link = db.get(FreshKeyProject, req.project_id)
    if not actor or not actor.email or not link:
        return False
    app_label = APP_LABELS.get(req.target_app_code, req.target_app_code.title())
    state = "potvrzeno" if accepted else "odmítnuto"
    text = f"Propojení projektu {link.name} s aplikací {app_label} bylo {state}."
    try:
        send_email(db, actor.email, f"Propojení {state} · {link.name}", text, f"<p>{html.escape(text)}</p>")
        return True
    except Exception:
        return False


def _link_request_payload(db, req: FreshKeyLinkRequest) -> dict:
    link = db.get(FreshKeyProject, req.project_id)
    actor = db.get(FreshKeyIdentity, req.requested_by_identity_id)
    local = db.get(FreshKeyLocalProject, req.target_local_project_id) if req.target_local_project_id else None
    return {
        "id": req.id,
        "fresh_key_id": req.project_id,
        "link_name": link.name if link else "",
        "request_type": req.request_type,
        "target_app_code": req.target_app_code,
        "target_app_label": APP_LABELS.get(req.target_app_code, req.target_app_code.title()),
        "target_local_project_id": req.target_local_project_id or "",
        "target_external_project_id": local.external_project_id if local else "",
        "target_project_name": local.external_project_name if local else req.requested_project_name,
        "requested_project_url": req.requested_project_url,
        "status": req.status,
        "message": req.message,
        "requester": {
            "identity_id": actor.id if actor else "",
            "email": actor.email if actor else "",
            "display_name": actor.display_name if actor else "",
        },
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        "responded_at": req.responded_at.isoformat() if req.responded_at else None,
        "result_external_project_id": req.result_external_project_id,
        "action_url": _app_request_url(req.target_app_code, req.id, local),
    }


def create_link_request(db, *, actor_identity_id: str, project_id: str, target_app_code: str,
                        request_type: str, target_local_project_id: str = "",
                        requested_project_name: str = "", requested_project_url: str = "",
                        message: str = "") -> FreshKeyLinkRequest:
    _require_member(db, actor_identity_id, project_id, manage=True)
    app_code = str(target_app_code or "").strip().lower()
    if app_code == "webbooster":
        app_code = "boban"
    if not APP_CODE_RE.fullmatch(app_code) or app_code == "ai-engine":
        raise ValueError("Vyber platnou cílovou Fresh aplikaci.")
    # The workflow starts from the same Fresh identity: Engine only proposes the
    # action; the target app account must already be linked to this identity.
    app_binding = db.scalar(select(FreshKeyIdentityBinding).where(
        FreshKeyIdentityBinding.identity_id == actor_identity_id,
        FreshKeyIdentityBinding.app_code == app_code,
    ))
    if not app_binding:
        raise ValueError(f"Pod tímto Fresh účtem zatím není propojen účet v aplikaci {APP_LABELS.get(app_code, app_code)}.")
    if db.scalar(select(FreshKeyProjectBinding).where(
        FreshKeyProjectBinding.project_id == project_id,
        FreshKeyProjectBinding.app_code == app_code,
    )):
        raise ValueError("Tato aplikace už je k Fresh Key připojená.")
    existing_pending = db.scalar(select(FreshKeyLinkRequest).where(
        FreshKeyLinkRequest.project_id == project_id,
        FreshKeyLinkRequest.target_identity_id == actor_identity_id,
        FreshKeyLinkRequest.target_app_code == app_code,
        FreshKeyLinkRequest.status == "pending",
    ))
    if existing_pending:
        raise ValueError("Pro tuto aplikaci už čeká návrh na potvrzení.")

    request_type = str(request_type or "attach_existing").strip().lower()
    local = None
    if request_type == "attach_existing":
        if not target_local_project_id:
            raise ValueError("Vyber projekt v cílové aplikaci.")
        local = _require_local_access(db, actor_identity_id, target_local_project_id)
        if local.app_code != app_code:
            raise ValueError("Vybraný projekt nepatří do zvolené aplikace.")
        if _local_project_binding(db, local):
            raise ValueError("Vybraný projekt už je propojený přes Fresh Key.")
        requested_project_name = local.external_project_name
        requested_project_url = local.website_url
    elif request_type == "create_new":
        requested_project_name = str(requested_project_name or "").strip()[:190]
        requested_project_url = str(requested_project_url or "").strip()[:500]
        if not requested_project_name:
            raise ValueError("Zadej název nového projektu.")
    else:
        raise ValueError("Neplatný typ návrhu propojení.")

    req = FreshKeyLinkRequest(
        project_id=project_id,
        requested_by_identity_id=actor_identity_id,
        target_identity_id=actor_identity_id,
        target_app_code=app_code,
        request_type=request_type,
        target_local_project_id=local.id if local else None,
        requested_project_name=requested_project_name,
        requested_project_url=requested_project_url,
        status="pending",
        message=str(message or "")[:1000],
        expires_at=utcnow() + timedelta(days=14),
    )
    db.add(req); db.commit()
    _send_link_request_email(db, req)
    return req


def list_link_requests_for_app(db, *, identity_id: str, app_code: str, status: str = "pending") -> list[dict]:
    app_code = str(app_code or "").strip().lower()
    q = select(FreshKeyLinkRequest).where(
        FreshKeyLinkRequest.target_identity_id == identity_id,
        FreshKeyLinkRequest.target_app_code == app_code,
    )
    if status:
        q = q.where(FreshKeyLinkRequest.status == status)
    rows_ = db.scalars(q.order_by(FreshKeyLinkRequest.created_at.desc())).all()
    now = utcnow()
    out = []
    for row in rows_:
        exp = row.expires_at
        if row.status == "pending" and exp:
            exp_aware = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp
            if exp_aware < now:
                row.status = "expired"
                continue
        out.append(_link_request_payload(db, row))
    db.commit()
    return out


def list_link_requests_for_link(db, *, identity_id: str, project_id: str) -> list[dict]:
    _require_member(db, identity_id, project_id)
    rows_ = db.scalars(select(FreshKeyLinkRequest).where(
        FreshKeyLinkRequest.project_id == project_id
    ).order_by(FreshKeyLinkRequest.created_at.desc()).limit(30)).all()
    return [_link_request_payload(db, row) for row in rows_]


def accept_link_request(db, *, identity_id: str, app_code: str, request_id: str,
                        external_project_id: str = "", name: str = "", website_url: str = "",
                        organization_name: str = "", role: str = "member") -> FreshKeyLinkRequest:
    req = db.get(FreshKeyLinkRequest, request_id)
    if not req or req.status != "pending":
        raise ValueError("Návrh propojení už není aktivní.")
    app_code = str(app_code or "").strip().lower()
    if app_code == "webbooster":
        app_code = "boban"
    if req.target_identity_id != identity_id or req.target_app_code != app_code:
        raise PermissionError("Tento návrh nepatří tomuto účtu nebo aplikaci.")
    exp = req.expires_at
    if exp:
        exp_aware = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp
        if exp_aware < utcnow():
            req.status = "expired"; db.commit()
            raise ValueError("Návrh propojení už vypršel.")
    link = db.get(FreshKeyProject, req.project_id)
    if not link or not link.active:
        raise ValueError("Fresh Key propojení už neexistuje.")
    _require_member(db, identity_id, link.id)
    if db.scalar(select(FreshKeyProjectBinding).where(
        FreshKeyProjectBinding.project_id == link.id,
        FreshKeyProjectBinding.app_code == app_code,
    )):
        raise ValueError("Tato aplikace už je k propojení připojená.")

    if req.request_type == "attach_existing":
        local = db.get(FreshKeyLocalProject, req.target_local_project_id) if req.target_local_project_id else None
        if not local:
            raise ValueError("Vybraný projekt v cílové aplikaci už není dostupný.")
        _require_local_access(db, identity_id, local.id)
        if external_project_id and str(local.external_project_id) != str(external_project_id):
            raise ValueError("Potvrzený projekt neodpovídá návrhu Fresh Key.")
    else:
        if not external_project_id or not name:
            raise ValueError("Cílová aplikace musí nejdřív vytvořit projekt a vrátit jeho ID a název.")
        local = sync_local_project(
            db,
            identity_id=identity_id,
            app_code=app_code,
            external_project_id=str(external_project_id),
            name=str(name),
            website_url=str(website_url or req.requested_project_url),
            organization_name=str(organization_name or ""),
            role=role,
        )
    _attach_local_to_link(db, identity_id=identity_id, link=link, local=local)
    req.status = "accepted"
    req.responded_at = utcnow()
    req.responder_identity_id = identity_id
    req.result_external_project_id = str(local.external_project_id or "")[:120]
    db.commit()
    _send_link_result_email(db, req, True)
    return req


def decline_link_request(db, *, identity_id: str, app_code: str, request_id: str) -> FreshKeyLinkRequest:
    req = db.get(FreshKeyLinkRequest, request_id)
    if not req or req.status != "pending":
        raise ValueError("Návrh propojení už není aktivní.")
    app_code = str(app_code or "").strip().lower()
    if app_code == "webbooster":
        app_code = "boban"
    if req.target_identity_id != identity_id or req.target_app_code != app_code:
        raise PermissionError("Tento návrh nepatří tomuto účtu nebo aplikaci.")
    req.status = "declined"
    req.responded_at = utcnow()
    req.responder_identity_id = identity_id
    db.commit()
    _send_link_result_email(db, req, False)
    return req

def _upsert_asset(db, provider: str, asset_type: str, external_id: str, name: str, metadata: dict) -> FreshKeyAsset:
    asset = db.scalar(select(FreshKeyAsset).where(
        FreshKeyAsset.provider == provider,
        FreshKeyAsset.asset_type == asset_type,
        FreshKeyAsset.external_id == str(external_id),
    ))
    if not asset:
        asset = FreshKeyAsset(provider=provider, asset_type=asset_type, external_id=str(external_id), name=(name or str(external_id))[:255])
        db.add(asset)
        db.flush()
    asset.name = (name or asset.name or str(external_id))[:255]
    asset.metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
    return asset


def _asset_auth(db, credential: FreshKeyOAuthCredential, asset: FreshKeyAsset, access_token: str = "", scopes: str = "", metadata: dict | None = None) -> FreshKeyAssetAuthorization:
    row = db.scalar(select(FreshKeyAssetAuthorization).where(
        FreshKeyAssetAuthorization.credential_id == credential.id,
        FreshKeyAssetAuthorization.asset_id == asset.id,
    ))
    if not row:
        row = FreshKeyAssetAuthorization(credential_id=credential.id, asset_id=asset.id)
        db.add(row)
    row.access_token_encrypted = encrypt_secret(access_token) if access_token else ""
    row.scopes = scopes or credential.scopes or ""
    row.metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
    db.flush()
    return row


def _new_credential(db, *, provider: str, identity_id: str, subject: str, name: str,
                    access_token: str, refresh_token: str = "", expires_in: int = 0,
                    scopes: str = "", metadata: dict | None = None) -> FreshKeyOAuthCredential:
    expires_at = utcnow() + timedelta(seconds=max(60, int(expires_in) - 60)) if expires_in else None
    row = FreshKeyOAuthCredential(
        provider=provider, authenticated_by_identity_id=identity_id,
        provider_subject=(subject or "")[:255], provider_name=(name or "")[:190],
        access_token_encrypted=encrypt_secret(access_token), refresh_token_encrypted=encrypt_secret(refresh_token),
        expires_at=expires_at, scopes=scopes or "", metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
    )
    db.add(row); db.flush(); return row


def _transaction_by_state(db, raw_state: str) -> FreshKeyOAuthTransaction | None:
    if not raw_state:
        return None
    digest = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    row = db.scalar(select(FreshKeyOAuthTransaction).where(FreshKeyOAuthTransaction.state_hash == digest))
    if not row:
        return None
    exp = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if exp < utcnow() or row.status in {"completed", "cancelled", "expired"}:
        return None
    return row


def _allowed_return_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return settings.fresh_key_public_origin.rstrip("/") + value
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Fresh Key return URL musí být HTTPS.")
    allowed = [x.strip().lower() for x in (settings.fresh_key_allowed_return_hosts or ".freshcrackers.cz").split(",") if x.strip()]
    host = parts.hostname.lower()
    if not any(host == item.lstrip(".") or (item.startswith(".") and host.endswith(item)) for item in allowed):
        raise ValueError("Return URL není v povolených Fresh Key doménách.")
    return value


def _callback_url(provider: str) -> str:
    return settings.fresh_key_public_origin.rstrip("/") + f"/fresh-key/oauth/{provider}/callback"


def start_oauth(db, *, identity_id: str, project_id: str, provider: str, return_url: str, app_code: str) -> dict:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Nepodporovaný Fresh Key provider.")
    _require_member(db, identity_id, project_id, manage=True)
    if not _provider_configured(provider):
        raise RuntimeError(f"Provider {provider} ještě není nakonfigurován ve Fresh Key.")
    return_url = _allowed_return_url(return_url)
    raw_state = secrets.token_urlsafe(36)
    tx = FreshKeyOAuthTransaction(
        state_hash=hashlib.sha256(raw_state.encode("utf-8")).hexdigest(), identity_id=identity_id,
        project_id=project_id, provider=provider, app_code=app_code, return_url=return_url,
        expires_at=utcnow() + timedelta(minutes=20),
    )
    db.add(tx); db.commit()
    if provider == "meta":
        scopes = [x.strip() for x in settings.fresh_key_meta_scopes.replace(" ", ",").split(",") if x.strip()]
        params = {"client_id": settings.fresh_key_meta_client_id, "redirect_uri": _callback_url(provider),
                  "state": raw_state, "scope": ",".join(scopes), "response_type": "code"}
        url = f"https://www.facebook.com/{settings.fresh_key_meta_api_version}/dialog/oauth?" + urlencode(params)
    elif provider == "linkedin":
        scopes = " ".join(settings.fresh_key_linkedin_scopes.replace(",", " ").split())
        params = {"response_type": "code", "client_id": settings.fresh_key_linkedin_client_id,
                  "redirect_uri": _callback_url(provider), "state": raw_state, "scope": scopes}
        url = "https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params)
    else:
        params = {"client_id": settings.fresh_key_google_client_id, "redirect_uri": _callback_url(provider),
                  "response_type": "code", "scope": settings.fresh_key_google_scopes,
                  "access_type": "offline", "include_granted_scopes": "true", "prompt": "consent", "state": raw_state}
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return {"authorization_url": url, "provider": provider, "expires_in": 1200}


def _http_json(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    response = client.request(method, url, **kwargs)
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"Provider vrátil neplatný JSON: {response.text[:300]}") from exc


def _discover_meta(db, tx, code: str) -> tuple[FreshKeyOAuthCredential, list[str]]:
    version = settings.fresh_key_meta_api_version
    callback = _callback_url("meta")
    with httpx.Client(timeout=40, follow_redirects=True) as client:
        token = _http_json(client, "GET", f"https://graph.facebook.com/{version}/oauth/access_token", params={
            "client_id": settings.fresh_key_meta_client_id, "client_secret": settings.fresh_key_meta_client_secret,
            "redirect_uri": callback, "code": code,
        })
        access = str(token.get("access_token") or "")
        if not access: raise RuntimeError("Meta nevrátil access token.")
        try:
            long_lived = _http_json(client, "GET", f"https://graph.facebook.com/{version}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": settings.fresh_key_meta_client_id,
                "client_secret": settings.fresh_key_meta_client_secret, "fb_exchange_token": access,
            })
            access = str(long_lived.get("access_token") or access)
            expires_in = int(long_lived.get("expires_in") or token.get("expires_in") or 0)
        except Exception:
            expires_in = int(token.get("expires_in") or 0)
        me = _http_json(client, "GET", f"https://graph.facebook.com/{version}/me", params={"fields": "id,name", "access_token": access})
        pages = _http_json(client, "GET", f"https://graph.facebook.com/{version}/me/accounts", params={
            "fields": "id,name,access_token,tasks,instagram_business_account{id,username}", "limit": 100, "access_token": access,
        })
    scopes = settings.fresh_key_meta_scopes
    credential = _new_credential(db, provider="meta", identity_id=tx.identity_id, subject=str(me.get("id") or ""),
                                 name=str(me.get("name") or "Meta účet"), access_token=access,
                                 expires_in=expires_in, scopes=scopes)
    asset_ids: list[str] = []
    for page in pages.get("data") or []:
        page_id = str(page.get("id") or "").strip(); page_token = str(page.get("access_token") or "")
        if not page_id: continue
        asset = _upsert_asset(db, "meta", "facebook_page", page_id, str(page.get("name") or page_id), {
            "tasks": page.get("tasks") or [], "page_id": page_id,
        })
        _asset_auth(db, credential, asset, page_token, scopes, {"token_kind": "page"})
        asset_ids.append(asset.id)
        ig = page.get("instagram_business_account") or {}
        ig_id = str(ig.get("id") or "").strip()
        if ig_id:
            ig_name = str(ig.get("username") or page.get("name") or ig_id)
            ig_asset = _upsert_asset(db, "meta", "instagram_account", ig_id, "@" + ig_name.lstrip("@"), {
                "instagram_id": ig_id, "facebook_page_id": page_id,
            })
            _asset_auth(db, credential, ig_asset, page_token, scopes, {"token_kind": "page", "facebook_page_id": page_id})
            asset_ids.append(ig_asset.id)
    db.flush()
    return credential, asset_ids


def _discover_linkedin(db, tx, code: str) -> tuple[FreshKeyOAuthCredential, list[str]]:
    callback = _callback_url("linkedin")
    with httpx.Client(timeout=40, follow_redirects=True) as client:
        token = _http_json(client, "POST", "https://www.linkedin.com/oauth/v2/accessToken", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": callback,
            "client_id": settings.fresh_key_linkedin_client_id, "client_secret": settings.fresh_key_linkedin_client_secret,
        }, headers={"Accept": "application/json"})
        access = str(token.get("access_token") or ""); refresh = str(token.get("refresh_token") or "")
        if not access: raise RuntimeError("LinkedIn nevrátil access token.")
        profile = _http_json(client, "GET", "https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    sub = str(profile.get("sub") or "").strip()
    display = str(profile.get("name") or profile.get("email") or "LinkedIn profil")
    scopes = str(token.get("scope") or settings.fresh_key_linkedin_scopes)
    credential = _new_credential(db, provider="linkedin", identity_id=tx.identity_id, subject=sub, name=display,
                                 access_token=access, refresh_token=refresh, expires_in=int(token.get("expires_in") or 0), scopes=scopes)
    asset_ids: list[str] = []
    member_urn = sub if sub.startswith("urn:li:") else f"urn:li:person:{sub}"
    asset = _upsert_asset(db, "linkedin", "linkedin_member", member_urn, display, {"author_urn": member_urn})
    _asset_auth(db, credential, asset, "", scopes)
    asset_ids.append(asset.id)
    # Company Pages require Community Management permissions. Discover them only
    # when the app was approved; lack of approval must not break personal OAuth.
    requested = set(settings.fresh_key_linkedin_scopes.replace(",", " ").split())
    if requested & {"rw_organization_admin", "r_organization_admin", "w_organization_social"}:
        try:
            headers = {"Authorization": f"Bearer {access}", "Accept": "application/json",
                       "X-Restli-Protocol-Version": "2.0.0", "Linkedin-Version": settings.fresh_key_linkedin_version}
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                acl = _http_json(client, "GET", "https://api.linkedin.com/rest/organizationAcls",
                                 params={"q": "roleAssignee", "state": "APPROVED"}, headers=headers)
                for item in acl.get("elements") or []:
                    org = str(item.get("organization") or item.get("organizationalTarget") or "").strip()
                    if not org: continue
                    org_id = org.rsplit(":", 1)[-1]
                    name = f"LinkedIn Page {org_id}"
                    try:
                        org_data = _http_json(client, "GET", f"https://api.linkedin.com/rest/organizations/{org_id}", headers=headers)
                        localized = org_data.get("localizedName") or org_data.get("vanityName")
                        if localized: name = str(localized)
                    except Exception:
                        pass
                    org_urn = org if org.startswith("urn:li:") else f"urn:li:organization:{org_id}"
                    org_asset = _upsert_asset(db, "linkedin", "linkedin_organization", org_urn, name, {"author_urn": org_urn})
                    _asset_auth(db, credential, org_asset, "", scopes)
                    asset_ids.append(org_asset.id)
        except Exception:
            pass
    db.flush(); return credential, asset_ids


def _discover_google(db, tx, code: str) -> tuple[FreshKeyOAuthCredential, list[str]]:
    callback = _callback_url("google_business")
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        token = _http_json(client, "POST", "https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": callback,
            "client_id": settings.fresh_key_google_client_id, "client_secret": settings.fresh_key_google_client_secret,
        }, headers={"Accept": "application/json"})
        access = str(token.get("access_token") or ""); refresh = str(token.get("refresh_token") or "")
        if not access: raise RuntimeError("Google nevrátil access token.")
        headers = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
        try:
            userinfo = _http_json(client, "GET", "https://openidconnect.googleapis.com/v1/userinfo", headers=headers)
        except Exception:
            userinfo = {}
        accounts = _http_json(client, "GET", "https://mybusinessaccountmanagement.googleapis.com/v1/accounts", headers=headers)
        locations: list[dict] = []
        for account in (accounts.get("accounts") or [])[:40]:
            account_name = str(account.get("name") or "")
            if not account_name: continue
            try:
                data = _http_json(client, "GET", f"https://mybusinessbusinessinformation.googleapis.com/v1/{account_name}/locations",
                                  params={"readMask": "name,title,storeCode,websiteUri", "pageSize": 100}, headers=headers)
                for loc in data.get("locations") or []:
                    locations.append({"account": account_name, **loc})
            except Exception:
                continue
    scopes = str(token.get("scope") or settings.fresh_key_google_scopes)
    credential = _new_credential(db, provider="google_business", identity_id=tx.identity_id,
                                 subject=str(userinfo.get("sub") or userinfo.get("email") or ""),
                                 name=str(userinfo.get("name") or userinfo.get("email") or "Google účet"),
                                 access_token=access, refresh_token=refresh, expires_in=int(token.get("expires_in") or 0), scopes=scopes)
    asset_ids: list[str] = []
    for loc in locations:
        loc_name = str(loc.get("name") or "")
        account_name = str(loc.get("account") or "")
        if not loc_name: continue
        external_id = f"{account_name}|{loc_name}"
        asset = _upsert_asset(db, "google_business", "google_business_location", external_id,
                              str(loc.get("title") or loc_name), {
                                  "account_resource": account_name, "location_resource": loc_name,
                                  "store_code": loc.get("storeCode") or "", "website": loc.get("websiteUri") or "",
                              })
        _asset_auth(db, credential, asset, "", scopes)
        asset_ids.append(asset.id)
    db.flush(); return credential, asset_ids


def _callback(provider: str):
    raw_state = str(request.args.get("state") or "")
    code = str(request.args.get("code") or "")
    error = str(request.args.get("error") or "")
    error_desc = str(request.args.get("error_description") or request.args.get("error_reason") or "")
    with SessionLocal() as db:
        tx = _transaction_by_state(db, raw_state)
        if not tx or tx.provider != provider:
            return render_template("fresh_key_error.html", message="Fresh Key autorizaci nebylo možné ověřit."), 400
        if error or not code:
            tx.status = "cancelled"; db.commit()
            return render_template("fresh_key_error.html", message=f"Autorizace byla zrušena: {error_desc or error or 'provider nevrátil kód'}"), 400
        try:
            if provider == "meta": credential, asset_ids = _discover_meta(db, tx, code)
            elif provider == "linkedin": credential, asset_ids = _discover_linkedin(db, tx, code)
            else: credential, asset_ids = _discover_google(db, tx, code)
            tx.credential_id = credential.id
            tx.discovered_asset_ids_json = json.dumps(asset_ids)
            tx.status = "choose"
            db.commit()
        except Exception as exc:
            tx.status = "error"; db.commit()
            return render_template("fresh_key_error.html", message=f"Fresh Key připojení selhalo: {exc}"), 400
    return redirect("/fresh-key/oauth/choose?state=" + raw_state, code=303)


def _append_query(url: str, values: dict[str, str]) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True)); q.update(values)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _connection_dict(db, connection: FreshKeyProjectConnection, include_secret: bool = False) -> dict:
    asset = db.get(FreshKeyAsset, connection.asset_id)
    credential = db.get(FreshKeyOAuthCredential, connection.credential_id)
    auth = db.get(FreshKeyAssetAuthorization, connection.asset_authorization_id) if connection.asset_authorization_id else None
    metadata = _json(asset.metadata_json if asset else "{}", {})
    provider = asset.provider if asset else (credential.provider if credential else "")
    mapped_provider = provider
    if provider == "meta" and asset:
        mapped_provider = "instagram" if asset.asset_type == "instagram_account" else "facebook"
    data = {
        "id": connection.id, "project_id": connection.project_id, "provider": mapped_provider,
        "provider_group": provider, "asset_id": asset.id if asset else None,
        "asset_type": asset.asset_type if asset else "", "external_id": asset.external_id if asset else "",
        "account_name": asset.name if asset else "", "status": connection.status,
        "capabilities": _json(connection.capabilities_json, []), "metadata": metadata,
        "created_via": connection.created_via,
    }
    if include_secret and credential:
        credential = _refresh_google_if_needed(db, credential) if provider == "google_business" else credential
        token = decrypt_secret(auth.access_token_encrypted) if auth and auth.access_token_encrypted else decrypt_secret(credential.access_token_encrypted)
        data.update({"access_token": token,
                     "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
                     "scopes": auth.scopes if auth and auth.scopes else credential.scopes,
                     "auth_user": _runtime_auth_user(asset, metadata)})
    return data


def _runtime_auth_user(asset: FreshKeyAsset | None, metadata: dict) -> str:
    if not asset: return ""
    if asset.asset_type in {"facebook_page", "instagram_account"}: return asset.external_id
    if asset.asset_type.startswith("linkedin_"): return str(metadata.get("author_urn") or asset.external_id)
    if asset.asset_type == "google_business_location":
        account = str(metadata.get("account_resource") or "").replace("accounts/", "")
        location = str(metadata.get("location_resource") or "").replace("locations/", "")
        return f"{account}|{location}"
    return asset.external_id


def _provider_client_id(provider: str) -> str:
    return {"meta": settings.fresh_key_meta_client_id, "linkedin": settings.fresh_key_linkedin_client_id,
            "google_business": settings.fresh_key_google_client_id}.get(provider, "")


def _refresh_google_if_needed(db, credential: FreshKeyOAuthCredential) -> FreshKeyOAuthCredential:
    if credential.provider != "google_business" or not credential.refresh_token_encrypted:
        return credential
    exp = credential.expires_at
    if exp is not None:
        exp = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp
        if exp > utcnow() + timedelta(minutes=3): return credential
    refresh = decrypt_secret(credential.refresh_token_encrypted)
    with httpx.Client(timeout=30) as client:
        data = _http_json(client, "POST", "https://oauth2.googleapis.com/token", data={
            "client_id": settings.fresh_key_google_client_id, "client_secret": settings.fresh_key_google_client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token",
        })
    access = str(data.get("access_token") or "")
    if not access: return credential
    credential.access_token_encrypted = encrypt_secret(access)
    credential.expires_at = utcnow() + timedelta(seconds=max(60, int(data.get("expires_in") or 3600) - 60))
    db.commit(); return credential


def list_project_connections(db, identity_id: str, project_id: str) -> list[dict]:
    _require_member(db, identity_id, project_id)
    rows = db.scalars(select(FreshKeyProjectConnection).where(
        FreshKeyProjectConnection.project_id == project_id,
        FreshKeyProjectConnection.status != "deleted",
    ).order_by(FreshKeyProjectConnection.created_at)).all()
    return [_connection_dict(db, row) for row in rows]


def _bind_assets(db, tx: FreshKeyOAuthTransaction, selected_ids: list[str]) -> list[str]:
    _require_member(db, tx.identity_id, tx.project_id, manage=True)
    discovered = set(_json(tx.discovered_asset_ids_json, []))
    selected = [x for x in selected_ids if x in discovered]
    if not selected:
        raise ValueError("Vyber alespoň jeden účet/profil.")
    connection_ids = []
    for asset_id in selected:
        asset = db.get(FreshKeyAsset, asset_id)
        if not asset: continue
        auth = db.scalar(select(FreshKeyAssetAuthorization).where(
            FreshKeyAssetAuthorization.credential_id == tx.credential_id,
            FreshKeyAssetAuthorization.asset_id == asset_id,
        ))
        row = db.scalar(select(FreshKeyProjectConnection).where(
            FreshKeyProjectConnection.project_id == tx.project_id,
            FreshKeyProjectConnection.asset_id == asset_id,
        ))
        caps = ["read"]
        if asset.asset_type in {"facebook_page", "instagram_account", "linkedin_member", "linkedin_organization", "google_business_location"}:
            caps.append("publish")
        if not row:
            row = FreshKeyProjectConnection(project_id=tx.project_id, asset_id=asset_id, credential_id=tx.credential_id,
                                            asset_authorization_id=auth.id if auth else None,
                                            connected_by_identity_id=tx.identity_id, created_via=tx.app_code,
                                            capabilities_json=json.dumps(caps))
            db.add(row); db.flush()
        else:
            row.credential_id = tx.credential_id
            row.asset_authorization_id = auth.id if auth else None
            row.connected_by_identity_id = tx.identity_id
            row.created_via = tx.app_code
            row.capabilities_json = json.dumps(caps)
            row.status = "connected"; row.last_error = ""
        connection_ids.append(row.id)
    tx.status = "completed"; tx.completed_at = utcnow(); db.commit()
    return connection_ids


def _api_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _safe_sso_pending() -> dict | None:
    pending = session.get("fresh_key_sso_pending")
    if not isinstance(pending, dict):
        return None
    try:
        created = int(pending.get("created_at") or 0)
    except Exception:
        return None
    if int(utcnow().timestamp()) - created > 600:
        session.pop("fresh_key_sso_pending", None)
        return None
    return pending


def sso_authorize():
    app_code = str(request.args.get("app_code") or "").strip().lower()
    raw_state = str(request.args.get("state") or "").strip()
    return_url = str(request.args.get("return_url") or "").strip()
    if not APP_CODE_RE.fullmatch(app_code) or len(raw_state) < 16:
        return render_template("fresh_key_error.html", message="Fresh Key přihlášení nemá platný stav aplikace."), 400
    try:
        return_url = _allowed_return_url(return_url)
    except Exception as exc:
        return render_template("fresh_key_error.html", message=str(exc)), 400
    session["fresh_key_sso_pending"] = {
        "app_code": app_code, "state": raw_state, "return_url": return_url,
        "created_at": int(utcnow().timestamp()),
    }
    if not session.get("customer_account_id"):
        session["customer_login_next"] = "/fresh-key/sso/continue"
        return redirect("/account/login?next=/fresh-key/sso/continue", code=303)
    return redirect("/fresh-key/sso/continue", code=303)


def sso_continue():
    pending = _safe_sso_pending()
    if not pending:
        return render_template("fresh_key_error.html", message="Fresh Key přihlášení vypršelo. Spusť ho z aplikace znovu."), 400
    with SessionLocal() as db:
        account = db.get(CustomerAccount, session.get("customer_account_id"))
        if not account:
            return redirect("/account/login", code=303)
        try:
            identity = ensure_identity_for_customer(db, account)
        except Exception as exc:
            return render_template("fresh_key_error.html", message=str(exc)), 403
        raw_code = secrets.token_urlsafe(42)
        row = FreshKeySsoCode(
            code_hash=hashlib.sha256(raw_code.encode("utf-8")).hexdigest(),
            identity_id=identity.id, app_code=pending["app_code"],
            expires_at=utcnow() + timedelta(minutes=3),
        )
        db.add(row); db.commit()
    session.pop("fresh_key_sso_pending", None)
    target = _append_query(pending["return_url"], {"fresh_key_code": raw_code, "state": pending["state"]})
    return redirect(target, code=303)


def api_sso_exchange():
    body = request.get_json(silent=True) or {}
    raw_code = str(body.get("code") or "").strip()
    app_code = _app_code(body)
    if not raw_code:
        return _api_error("Chybí Fresh Key přihlašovací kód.", 400)
    digest = hashlib.sha256(raw_code.encode("utf-8")).hexdigest()
    with SessionLocal() as db:
        row = db.scalar(select(FreshKeySsoCode).where(FreshKeySsoCode.code_hash == digest))
        if not row or row.app_code != app_code or row.used_at is not None:
            return _api_error("Fresh Key přihlašovací kód je neplatný nebo už použitý.", 403)
        exp = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
        if exp < utcnow():
            return _api_error("Fresh Key přihlašovací kód vypršel.", 403)
        identity = db.get(FreshKeyIdentity, row.identity_id)
        if not identity or not identity.active or not identity.email_verified:
            return _api_error("Fresh Key identita není ověřená.", 403)
        existing = db.scalar(select(FreshKeyIdentityBinding).where(
            FreshKeyIdentityBinding.identity_id == identity.id, FreshKeyIdentityBinding.app_code == app_code
        ))
        raw_binding = ""
        row.used_at = utcnow()
        if not existing:
            raw_binding = secrets.token_urlsafe(42)
            row.binding_token_hash = hashlib.sha256(raw_binding.encode("utf-8")).hexdigest()
            row.binding_expires_at = utcnow() + timedelta(minutes=5)
        db.commit()
        return jsonify({
            "ok": True,
            "identity": {"id": identity.id, "email": identity.email, "display_name": identity.display_name},
            "external_user_id": existing.external_user_id if existing else None,
            "binding_token": raw_binding or None,
        })


def api_sso_bind():
    body = request.get_json(silent=True) or {}
    raw_token = str(body.get("binding_token") or "").strip()
    external_user_id = str(body.get("external_user_id") or "").strip()[:120]
    app_code = _app_code(body)
    if not raw_token or not external_user_id:
        return _api_error("Chybí údaje pro propojení Fresh Key identity.", 400)
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with SessionLocal() as db:
        row = db.scalar(select(FreshKeySsoCode).where(FreshKeySsoCode.binding_token_hash == digest))
        if not row or row.app_code != app_code or row.binding_used_at is not None:
            return _api_error("Fresh Key binding token je neplatný nebo už použitý.", 403)
        exp = row.binding_expires_at
        exp = exp.replace(tzinfo=timezone.utc) if exp and exp.tzinfo is None else exp
        if not exp or exp < utcnow():
            return _api_error("Fresh Key binding token vypršel.", 403)
        identity = db.get(FreshKeyIdentity, row.identity_id)
        if not identity or not identity.email_verified:
            return _api_error("Fresh Key identita není ověřená.", 403)
        identity_binding = db.scalar(select(FreshKeyIdentityBinding).where(
            FreshKeyIdentityBinding.identity_id == identity.id, FreshKeyIdentityBinding.app_code == app_code
        ))
        if identity_binding and identity_binding.external_user_id != external_user_id:
            return _api_error("Tato Fresh Key identita už je v aplikaci propojena s jiným uživatelem.", 409)
        binding = db.scalar(select(FreshKeyIdentityBinding).where(
            FreshKeyIdentityBinding.app_code == app_code, FreshKeyIdentityBinding.external_user_id == external_user_id
        ))
        if binding:
            old_identity = db.get(FreshKeyIdentity, binding.identity_id)
            if old_identity and old_identity.email_verified and old_identity.id != identity.id:
                return _api_error("Lokální účet je už propojen s jinou ověřenou Fresh Key identitou.", 409)
            binding.identity_id = identity.id
            binding.last_seen_at = utcnow()
        else:
            db.add(FreshKeyIdentityBinding(identity_id=identity.id, app_code=app_code, external_user_id=external_user_id))
        row.binding_used_at = utcnow()
        db.commit()
        return jsonify({"ok": True, "identity_id": identity.id})


def api_identity_resolve():
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db)
            return jsonify({"ok": True, "identity": {"id": identity.id, "email": identity.email, "display_name": identity.display_name, "email_verified": bool(identity.email_verified)}})
    except PermissionError as exc:
        return _api_error(str(exc), 404)
    except Exception as exc:
        return _api_error(str(exc), 400)


def register_fresh_key(app, api_guard, customer_guard):
    """Register Fresh Key routes exactly once per Flask application.

    0.7.1 exposes both the new /links/* terminology and legacy /projects/*
    compatibility aliases. Flask derives endpoint names from the wrapped
    function name, so registering the same view twice without explicit unique
    endpoints makes application startup fail. CloudLinux/Passenger may also
    reuse the imported module during reloads, therefore route construction is
    deliberately idempotent.
    """
    if "fresh_key" in app.blueprints:
        return

    routes_configured = bool(getattr(bp, "_fresh_key_routes_configured", False))
    if not routes_configured:
        # API used by Linda/Boban/etc. The app itself authenticates using the
        # normal Engine connector key; Fresh Key additionally checks project
        # membership. Every compatibility alias has its own endpoint name.
        bp.route("/v1/fresh-key/identity/sync", methods=["POST"], endpoint="api_identity_sync")(api_guard(api_identity_sync))
        bp.route("/v1/fresh-key/account/bootstrap", methods=["POST"], endpoint="api_account_bootstrap")(api_guard(api_account_bootstrap))
        bp.route("/v1/fresh-key/identity/resolve", methods=["GET"], endpoint="api_identity_resolve")(api_guard(api_identity_resolve))
        bp.route("/v1/fresh-key/project-share/notify", methods=["POST"], endpoint="api_project_share_notify")(api_guard(api_project_share_notify))
        bp.route("/v1/fresh-key/sso/exchange", methods=["POST"], endpoint="api_sso_exchange")(api_guard(api_sso_exchange))
        bp.route("/v1/fresh-key/sso/bind", methods=["POST"], endpoint="api_sso_bind")(api_guard(api_sso_bind))

        # v0.7.1 public terminology: local projects are only inventoried and
        # then grouped into Fresh Key links. /projects/* remains an alias.
        bp.route("/v1/fresh-key/local-projects/sync", methods=["POST"], endpoint="api_local_project_sync")(api_guard(api_local_project_sync))
        bp.route("/v1/fresh-key/local-projects", methods=["GET"], endpoint="api_local_projects")(api_guard(api_local_projects))
        bp.route("/v1/fresh-key/links/sync", methods=["POST"], endpoint="api_links_sync")(api_guard(api_project_sync))
        bp.route("/v1/fresh-key/links", methods=["GET"], endpoint="api_links")(api_guard(api_projects))
        bp.route("/v1/fresh-key/links/<project_id>/attach", methods=["POST"], endpoint="api_link_attach")(api_guard(api_link_attach))
        bp.route("/v1/fresh-key/links/<project_id>/connections", methods=["GET"], endpoint="api_link_connections")(api_guard(api_connections))
        bp.route("/v1/fresh-key/links/<project_id>/members", methods=["GET", "POST"], endpoint="api_link_members")(api_guard(api_project_members))
        bp.route("/v1/fresh-key/link-requests", methods=["GET"], endpoint="api_link_requests")(api_guard(api_link_requests))
        bp.route("/v1/fresh-key/link-requests/<request_id>/accept", methods=["POST"], endpoint="api_link_request_accept")(api_guard(api_link_request_accept))
        bp.route("/v1/fresh-key/link-requests/<request_id>/decline", methods=["POST"], endpoint="api_link_request_decline")(api_guard(api_link_request_decline))

        bp.route("/v1/fresh-key/projects/sync", methods=["POST"], endpoint="api_projects_sync_legacy")(api_guard(api_project_sync))
        bp.route("/v1/fresh-key/projects", methods=["GET"], endpoint="api_projects_legacy")(api_guard(api_projects))
        bp.route("/v1/fresh-key/projects/<project_id>/connections", methods=["GET"], endpoint="api_project_connections_legacy")(api_guard(api_connections))
        bp.route("/v1/fresh-key/projects/<project_id>/members", methods=["GET", "POST"], endpoint="api_project_members_legacy")(api_guard(api_project_members))

        bp.route("/v1/fresh-key/connections/<connection_id>/runtime", methods=["GET"], endpoint="api_connection_runtime")(api_guard(api_connection_runtime))
        bp.route("/v1/fresh-key/connections/<connection_id>/disconnect", methods=["POST"], endpoint="api_connection_disconnect")(api_guard(api_connection_disconnect))
        bp.route("/v1/fresh-key/oauth/<provider>/start", methods=["POST"], endpoint="api_oauth_start")(api_guard(api_oauth_start))

        # Fresh Key central login broker. authorize is public but only issues a
        # code after the Engine customer session authenticated a verified account.
        bp.route("/fresh-key/sso/authorize", methods=["GET"], endpoint="sso_authorize")(sso_authorize)
        bp.route("/fresh-key/sso/continue", methods=["GET"], endpoint="sso_continue")(customer_guard(sso_continue))

        # Customer UI in AI Engine. Same underlying service/data as app API.
        bp.route("/account/fresh-key", methods=["GET"], endpoint="account_fresh_key")(customer_guard(account_fresh_key))
        bp.route("/account/fresh-key/links", methods=["POST"], endpoint="account_link_create")(customer_guard(account_link_create))
        bp.route("/account/fresh-key/links/<project_id>/attach", methods=["POST"], endpoint="account_link_attach")(customer_guard(account_link_attach))
        bp.route("/account/fresh-key/links/<project_id>/members", methods=["POST"], endpoint="account_link_member_add")(customer_guard(account_member_add))
        bp.route("/account/fresh-key/links/<project_id>/requests", methods=["POST"], endpoint="account_link_request_create")(customer_guard(account_link_request_create))
        bp.route("/account/fresh-key/links/<project_id>/merge", methods=["POST"], endpoint="account_link_merge")(customer_guard(account_link_merge))
        bp.route("/account/fresh-key/links/<project_id>/detach", methods=["POST"], endpoint="account_link_detach")(customer_guard(account_link_detach))
        bp.route("/account/fresh-key/local-projects/<local_project_id>/resolve", methods=["POST"], endpoint="account_unlinked_resolve")(customer_guard(account_unlinked_resolve))
        bp.route("/account/fresh-key/links/<project_id>/oauth/<provider>", methods=["POST"], endpoint="account_link_oauth_start")(customer_guard(account_oauth_start))

        # Old form/action aliases are kept so a cached page cannot create a
        # duplicate, but they must use distinct Flask endpoint names.
        bp.route("/account/fresh-key/projects", methods=["POST"], endpoint="account_project_create_legacy")(customer_guard(account_project_create))
        bp.route("/account/fresh-key/projects/<project_id>/members", methods=["POST"], endpoint="account_project_member_add_legacy")(customer_guard(account_member_add))
        bp.route("/account/fresh-key/projects/<project_id>/oauth/<provider>", methods=["POST"], endpoint="account_project_oauth_start_legacy")(customer_guard(account_oauth_start))
        bp.route("/account/fresh-key/connections/<connection_id>/disconnect", methods=["POST"], endpoint="account_connection_disconnect")(customer_guard(account_connection_disconnect))

        setattr(bp, "_fresh_key_routes_configured", True)

    app.register_blueprint(bp)


def api_account_bootstrap():
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            result = bootstrap_engine_account(
                db, app_code=_app_code(body),
                external_user_id=str(body.get("external_user_id") or ""),
                email=str(body.get("email") or ""),
                display_name=str(body.get("display_name") or ""),
                email_verified=bool(body.get("email_verified", True)),
            )
            return jsonify({"ok": True, **result})
    except PermissionError as exc:
        return _api_error(str(exc), 403)
    except Exception as exc:
        return _api_error(str(exc), 400)


def api_identity_sync():
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = sync_external_identity(db, _app_code(body), body.get("external_user_id"), body.get("email"), body.get("display_name") or "", bool(body.get("email_verified")))
            return jsonify({"ok": True, "identity_id": identity.id, "email": identity.email, "display_name": identity.display_name})
    except Exception as exc:
        return _api_error(str(exc), 400)


def api_local_project_sync():
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            local = sync_local_project(
                db,
                identity_id=identity.id,
                app_code=_app_code(body),
                external_project_id=str(body.get("external_project_id") or ""),
                name=str(body.get("name") or ""),
                website_url=str(body.get("website_url") or ""),
                organization_name=str(body.get("organization_name") or ""),
                role=str(body.get("role") or "member"),
            )
            db.commit()
            binding = _local_project_binding(db, local)
            candidates = [] if binding else _matching_links_for_local(db, identity.id, local)
            return jsonify({"ok": True, "local_project": {
                "id": local.id,
                "app_code": local.app_code,
                "external_project_id": local.external_project_id,
                "name": local.external_project_name,
                "website_url": local.website_url,
                "organization_name": local.organization_name,
                "linked": bool(binding),
                "fresh_key_id": binding.project_id if binding else "",
                "candidate_links": [{"id": x.id, "name": x.name, "website_url": x.website_url} for x in candidates],
            }})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_local_projects():
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db)
            only_unlinked = str(request.args.get("unlinked") or "").lower() in {"1", "true", "yes"}
            return jsonify({"ok": True, "local_projects": list_local_projects(db, identity.id, unlinked_only=only_unlinked)})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_link_attach(project_id: str):
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            raw_ids = body.get("local_project_ids") or []
            if isinstance(raw_ids, str):
                raw_ids = [raw_ids]
            link = create_or_extend_link(
                db,
                identity_id=identity.id,
                local_project_ids=list(raw_ids),
                target_link_id=project_id,
            )
            return jsonify({"ok": True, "link": {"id": link.id, "name": link.name, "website_url": link.website_url}})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_project_sync():
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            duplicate_safe = bool(body.get("resolve_duplicates") or body.get("linking_version") == 2)
            if duplicate_safe:
                result = publish_local_project(
                    db, identity_id=identity.id, app_code=_app_code(body),
                    external_project_id=str(body.get("external_project_id") or ""),
                    name=str(body.get("name") or ""), website_url=str(body.get("website_url") or ""),
                    organization_name=str(body.get("organization_name") or ""),
                    fresh_project_id=str(body.get("fresh_key_link_id") or body.get("fresh_project_id") or ""),
                )
                link = result.get("link")
                local = result.get("local")
                payload = {"id": link.id, "name": link.name, "website_url": link.website_url} if link else None
                candidates = [{"id": x.id, "name": x.name, "website_url": x.website_url} for x in result.get("candidates", [])]
                manage = settings.fresh_key_public_origin.rstrip("/") + "/account/fresh-key"
                if local:
                    manage += "?local=" + str(local.id)
                return jsonify({
                    "ok": True, "status": result.get("status"), "link": payload, "project": payload,
                    "local_project_id": local.id if local else "", "candidates": candidates,
                    "manage_url": manage,
                })
            # 0.8 keeps the old publish contract for apps not upgraded yet. They
            # continue to work exactly as before; after their next update they can
            # opt into duplicate-safe publish using linking_version=2.
            project = sync_project(
                db, identity_id=identity.id, app_code=_app_code(body),
                external_project_id=str(body.get("external_project_id") or ""), name=str(body.get("name") or ""),
                website_url=str(body.get("website_url") or ""), organization_name=str(body.get("organization_name") or ""),
                fresh_project_id=str(body.get("fresh_key_link_id") or body.get("fresh_project_id") or ""),
            )
            payload = {"id": project.id, "name": project.name, "website_url": project.website_url}
            return jsonify({"ok": True, "status": "linked", "link": payload, "project": payload})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_projects():
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db)
            links = list_identity_projects(db, identity.id)
            return jsonify({"ok": True, "links": links, "projects": links})
    except Exception as exc: return _api_error(str(exc), 400)


def api_connections(project_id: str):
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db)
            return jsonify({"ok": True, "connections": list_project_connections(db, identity.id, project_id)})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_project_members(project_id: str):
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            if request.method == "GET":
                return jsonify({"ok": True, "members": list_project_members(db, identity.id, project_id)})
            action = str(body.get("action") or "upsert").strip().lower()
            if action == "remove":
                remove_project_member(db, actor_identity_id=identity.id, project_id=project_id,
                                      target_identity_id=str(body.get("identity_id") or ""),
                                      source_app_code=_app_code(body))
                return jsonify({"ok": True, "removed": True})
            if action == "update":
                member = update_project_member(
                    db, actor_identity_id=identity.id, project_id=project_id,
                    target_identity_id=str(body.get("identity_id") or ""),
                    role=str(body.get("role") or "member"),
                    can_manage_connections=bool(body.get("can_manage_connections", False) or str(body.get("role") or "").lower() == "manager"),
                    source_app_code=_app_code(body),
                )
            else:
                member = add_project_member(
                    db, actor_identity_id=identity.id, project_id=project_id,
                    email=str(body.get("email") or ""), role=str(body.get("role") or "member"),
                    can_manage_connections=bool(body.get("can_manage_connections", False) or str(body.get("role") or "").lower() == "manager"),
                    notify=bool(body.get("notify", True)),
                    source_app_code=_app_code(body),
                )
            target = db.get(FreshKeyIdentity, member.identity_id)
            return jsonify({"ok": True, "member": _member_payload(db, member, target) if target else {"identity_id": member.identity_id}})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)



def api_project_share_notify():
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            result = notify_app_project_share(
                db, actor_identity_id=identity.id,
                recipient_email=str(body.get("email") or ""),
                app_code=_app_code(body),
                project_name=str(body.get("project_name") or "Projekt"),
                role=str(body.get("role") or "member"),
                action_url=str(body.get("action_url") or ""),
            )
            return jsonify({"ok": True, **result})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_link_requests():
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db)
            status = str(request.args.get("status") or "pending").strip().lower()
            return jsonify({"ok": True, "requests": list_link_requests_for_app(
                db, identity_id=identity.id, app_code=_app_code(), status=status
            )})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_link_request_accept(request_id: str):
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            row = accept_link_request(
                db,
                identity_id=identity.id,
                app_code=_app_code(body),
                request_id=request_id,
                external_project_id=str(body.get("external_project_id") or ""),
                name=str(body.get("name") or ""),
                website_url=str(body.get("website_url") or ""),
                organization_name=str(body.get("organization_name") or ""),
                role=str(body.get("role") or "member"),
            )
            return jsonify({"ok": True, "request": _link_request_payload(db, row)})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_link_request_decline(request_id: str):
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            row = decline_link_request(db, identity_id=identity.id, app_code=_app_code(body), request_id=request_id)
            return jsonify({"ok": True, "request": _link_request_payload(db, row)})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)

def api_connection_runtime(connection_id: str):
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db)
            row = db.get(FreshKeyProjectConnection, connection_id)
            if not row: return _api_error("Fresh Key connection nenalezen.", 404)
            _require_member(db, identity.id, row.project_id)
            if row.status != "connected":
                return _api_error("Fresh Key connection už není aktivní.", 410)
            credential = db.get(FreshKeyOAuthCredential, row.credential_id)
            if not credential or credential.status != "connected":
                return _api_error("Fresh Key autorizace už není aktivní.", 410)
            return jsonify({"ok": True, "connection": _connection_dict(db, row, include_secret=True)})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_connection_disconnect(connection_id: str):
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            row = db.get(FreshKeyProjectConnection, connection_id)
            if not row: return _api_error("Fresh Key connection nenalezen.", 404)
            _require_member(db, identity.id, row.project_id, manage=True)
            row.status = "deleted"; db.commit()
            return jsonify({"ok": True})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


def api_oauth_start(provider: str):
    body = request.get_json(silent=True) or {}
    try:
        with SessionLocal() as db:
            identity = _identity_for_app_request(db, body)
            data = start_oauth(db, identity_id=identity.id, project_id=str(body.get("link_id") or body.get("project_id") or ""),
                               provider=provider, return_url=str(body.get("return_url") or ""), app_code=_app_code(body))
            return jsonify({"ok": True, **data})
    except PermissionError as exc: return _api_error(str(exc), 403)
    except Exception as exc: return _api_error(str(exc), 400)


@bp.get("/fresh-key/oauth/meta/callback")
def meta_callback(): return _callback("meta")


@bp.get("/fresh-key/oauth/linkedin/callback")
def linkedin_callback(): return _callback("linkedin")


@bp.get("/fresh-key/oauth/google_business/callback")
def google_callback(): return _callback("google_business")


@bp.route("/fresh-key/oauth/choose", methods=["GET", "POST"])
def oauth_choose():
    raw_state = str(request.args.get("state") or request.form.get("state") or "")
    with SessionLocal() as db:
        tx = _transaction_by_state(db, raw_state)
        if not tx or tx.status != "choose":
            return render_template("fresh_key_error.html", message="Výběr Fresh Key účtu vypršel nebo již byl dokončen."), 400
        asset_ids = _json(tx.discovered_asset_ids_json, [])
        assets = [db.get(FreshKeyAsset, x) for x in asset_ids]
        assets = [x for x in assets if x]
        project = db.get(FreshKeyProject, tx.project_id)
        if request.method == "POST":
            selected = request.form.getlist("asset_id")
            try:
                ids = _bind_assets(db, tx, selected)
                target = _append_query(tx.return_url, {"fresh_key": "connected", "fresh_key_link_id": tx.project_id,
                                                       "fresh_key_project_id": tx.project_id,
                                                       "fresh_key_connections": ",".join(ids)})
                return redirect(target, code=303)
            except Exception as exc:
                flash(str(exc), "error")
        return render_template("fresh_key_choose.html", transaction=tx, project=project, assets=assets, state=raw_state)


def _account_identity(db):
    account = db.get(CustomerAccount, session.get("customer_account_id"))
    if not account: abort(401)
    return account, ensure_identity_for_customer(db, account)


def account_fresh_key():
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        links = list_identity_projects(db, identity.id)
        all_apps = identity_apps(db, identity.id)
        for item in links:
            item["members"] = list_project_members(db, identity.id, item["id"])
            item["connections"] = list_project_connections(db, identity.id, item["id"])
            item["applications"] = [b for b in item.get("bindings", []) if b.get("app_code") != "ai-engine"]
            item["available_apps"] = [a for a in all_apps if not a.get("already_connected") or a.get("code") not in {b.get("app_code") for b in item["applications"]}]
            # identity_apps(project_id) also marks already_connected and carries all
            # local projects, including those already linked elsewhere. This is
            # intentional in 0.8: selecting such a project means merge/move, not
            # an empty dropdown.
            item["available_apps"] = [a for a in identity_apps(db, identity.id, item["id"]) if not a.get("already_connected")]
            item["link_requests"] = list_link_requests_for_link(db, identity_id=identity.id, project_id=item["id"])
        local_projects = list_local_projects(db, identity.id)
        unlinked = [x for x in local_projects if not x.get("linked")]
        for lp in unlinked:
            local = db.get(FreshKeyLocalProject, lp["id"])
            lp["candidate_links"] = [{"id": x.id, "name": x.name, "website_url": x.website_url} for x in (_matching_links_for_local(db, identity.id, local) if local else [])]
            lp["app_label"] = APP_LABELS.get(lp.get("app_code", ""), str(lp.get("app_code") or "").title())
        selected_id = str(request.args.get("link") or request.args.get("project") or (links[0]["id"] if links else ""))
        selected_local_id = str(request.args.get("local") or "")
        connections = list_project_connections(db, identity.id, selected_id) if selected_id else []
        members = list_project_members(db, identity.id, selected_id) if selected_id else []
        selected_link = next((p for p in links if p["id"] == selected_id), None)
        return render_template(
            "account_fresh_key.html", account=account, identity=identity, links=links, projects=links,
            local_projects=local_projects, unlinked_local_projects=unlinked, merge_suggestions=merge_suggestions(db, identity.id),
            selected_link_id=selected_id, selected_local_id=selected_local_id, selected_project_id=selected_id,
            selected_link=selected_link, selected_project=selected_link, connections=connections, members=members,
            providers=provider_status(), notice=request.args.get("notice", ""), error=request.args.get("error", ""),
        )


def account_link_create():
    local_ids = [str(x or "").strip() for x in request.form.getlist("local_project_id") if str(x or "").strip()]
    label = str(request.form.get("label") or "").strip()
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            link = create_or_extend_link(db, identity_id=identity.id, local_project_ids=local_ids, label=label)
            return redirect(f"/account/fresh-key?link={link.id}&notice=linked", code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"error": str(exc)[:180]}), code=303)


def account_link_attach(project_id: str):
    local_ids = [str(x or "").strip() for x in request.form.getlist("local_project_id") if str(x or "").strip()]
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            link = create_or_extend_link(
                db,
                identity_id=identity.id,
                local_project_ids=local_ids,
                target_link_id=project_id,
            )
            return redirect(f"/account/fresh-key?link={link.id}&notice=attached", code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"link": project_id, "error": str(exc)[:180]}), code=303)


def account_project_create():
    # Compatibility endpoint for cached 0.7.0 pages. AI Engine no longer creates
    # standalone projects; Fresh Key only links projects reported by Fresh apps.
    return redirect(_append_query("/account/fresh-key", {
        "error": "V AI Enginu se nové projekty nezakládají. Nejdřív otevři existující projekt v Lindě, Bobanovi, Karlovi nebo Edovi a synchronizuj ho do Fresh Key; tady ho potom pouze propojíš."
    }), code=303)


def account_member_add(project_id: str):
    # 0.7.3: Engine shows people only as a cross-app overview. Membership and
    # local roles are authoritative in Linda/Boban/Eda/etc. Keep this legacy
    # POST route only so cached 0.7.2 pages fail safely instead of mutating
    # central membership behind the source application's back.
    return redirect(_append_query(
        "/account/fresh-key",
        {"link": project_id, "error": "Sdílení a role uprav v konkrétní aplikaci, ne v AI Enginu."},
    ), code=303)



def account_link_request_create(project_id: str):
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            mode = str(request.form.get("request_type") or "attach_existing").strip().lower()
            target_local_id = str(request.form.get("target_local_project_id") or "").strip()
            if mode == "attach_existing" and target_local_id:
                local = _require_local_access(db, identity.id, target_local_id)
                binding = _local_project_binding(db, local)
                if binding and binding.project_id != project_id:
                    # The project has already been explicitly published from the
                    # target app. Selecting it here means the user is identifying
                    # two Fresh Key rows as the same project, so merge centrally.
                    if str(request.form.get("confirm_merge") or "") != "1":
                        raise ValueError("Vybraný projekt už patří do jiného Propojení. Potvrď sloučení obou Propojení.")
                    link = merge_links(db, identity_id=identity.id, target_link_id=project_id, source_link_id=binding.project_id)
                    return redirect(f"/account/fresh-key?link={link.id}&notice=merged", code=303)
            req = create_link_request(
                db, actor_identity_id=identity.id, project_id=project_id,
                target_app_code=str(request.form.get("target_app_code") or ""), request_type=mode,
                target_local_project_id=target_local_id,
                requested_project_name=str(request.form.get("requested_project_name") or ""),
                requested_project_url=str(request.form.get("requested_project_url") or ""),
                message=str(request.form.get("message") or ""),
            )
            return redirect(f"/account/fresh-key?link={project_id}&notice=request_sent", code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"link": project_id, "error": str(exc)[:200]}), code=303)


def account_link_merge(project_id: str):
    source_id = str(request.form.get("source_link_id") or "").strip()
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            link = merge_links(db, identity_id=identity.id, target_link_id=project_id, source_link_id=source_id)
            return redirect(f"/account/fresh-key?link={link.id}&notice=merged", code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"link": project_id, "error": str(exc)[:200]}), code=303)


def account_link_detach(project_id: str):
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            result = detach_local_from_link(
                db, identity_id=identity.id, project_id=project_id,
                app_code=str(request.form.get("app_code") or ""),
                external_project_id=str(request.form.get("external_project_id") or ""),
            )
            target = "/account/fresh-key" if result.get("archived") else f"/account/fresh-key?link={project_id}"
            return redirect(_append_query(target, {"notice": "detached"}), code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"link": project_id, "error": str(exc)[:200]}), code=303)


def account_unlinked_resolve(local_project_id: str):
    mode = str(request.form.get("mode") or "new").strip().lower()
    target_link_id = str(request.form.get("target_link_id") or "").strip()
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            local = _require_local_access(db, identity.id, local_project_id)
            if _local_project_binding(db, local):
                binding = _local_project_binding(db, local)
                return redirect(f"/account/fresh-key?link={binding.project_id}", code=303)
            if mode == "attach":
                if not target_link_id:
                    raise ValueError("Vyber Propojení.")
                link = create_or_extend_link(db, identity_id=identity.id, local_project_ids=[local.id], target_link_id=target_link_id)
                notice = "attached"
            else:
                link = create_or_extend_link(db, identity_id=identity.id, local_project_ids=[local.id])
                notice = "linked"
            return redirect(f"/account/fresh-key?link={link.id}&notice={notice}", code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"local": local_project_id, "error": str(exc)[:200]}), code=303)


def account_oauth_start(project_id: str, provider: str):
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        try:
            data = start_oauth(db, identity_id=identity.id, project_id=project_id, provider=provider,
                               return_url=f"{settings.fresh_key_public_origin.rstrip('/')}/account/fresh-key?link={project_id}", app_code="ai-engine")
            return redirect(data["authorization_url"], code=303)
        except Exception as exc:
            return redirect(_append_query("/account/fresh-key", {"link": project_id, "error": str(exc)[:160]}), code=303)


def account_connection_disconnect(connection_id: str):
    with SessionLocal() as db:
        account, identity = _account_identity(db)
        row = db.get(FreshKeyProjectConnection, connection_id)
        if not row: abort(404)
        try: _require_member(db, identity.id, row.project_id, manage=True)
        except PermissionError: abort(403)
        row.status = "deleted"; db.commit()
        return redirect(f"/account/fresh-key?link={row.project_id}&notice=disconnected", code=303)

