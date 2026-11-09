from __future__ import annotations
import hashlib, secrets
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, select, or_
from werkzeug.security import generate_password_hash, check_password_hash
from app.models import CustomerAccount, Connector, CreditLedger, CreditOrder, JobBilling, Job, ModelRoute, AccountEmailVerification, AccountPasswordReset


def utcnow(): return datetime.now(timezone.utc)

def hash_key(raw: str) -> str: return hashlib.sha256(raw.encode()).hexdigest()

def new_connector_key() -> str: return "fae_" + secrets.token_urlsafe(32)

def balance(db, account_id: str) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(CreditLedger.delta_credits), 0.0)).where(CreditLedger.account_id == account_id)) or 0.0)

def credit_setting(db, key: str, default: float) -> float:
    from app.models import RuntimeSetting
    row=db.get(RuntimeSetting,key)
    try: return float(row.value) if row else default
    except Exception: return default

def credits_per_usd(db): return credit_setting(db,'credits_per_usd',1000.0)
def credit_price_czk(db): return credit_setting(db,'credit_price_czk',0.10)

def create_account(db,email,password):
    email=email.strip().lower()
    if db.scalar(select(CustomerAccount).where(CustomerAccount.email==email)): raise ValueError('Účet s tímto e-mailem již existuje.')
    row=CustomerAccount(email=email,password_hash=generate_password_hash(password))
    db.add(row); db.commit(); db.refresh(row); return row

def verification_token(db, account_id, hours=24):
    raw=secrets.token_urlsafe(32); now=utcnow(); row=db.get(AccountEmailVerification, account_id)
    if row is None:
        row=AccountEmailVerification(account_id=account_id, token_hash=hash_key(raw), expires_at=now+timedelta(hours=hours), sent_at=now)
        db.add(row)
    else:
        row.token_hash=hash_key(raw); row.expires_at=now+timedelta(hours=hours); row.sent_at=now; row.verified_at=None
    db.commit(); return raw

def verify_email_token(db, raw):
    row=db.scalar(select(AccountEmailVerification).where(AccountEmailVerification.token_hash==hash_key(raw)))
    if not row or row.verified_at is not None: return None
    exp=row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if exp < utcnow(): return None
    row.verified_at=utcnow(); db.commit(); return db.get(CustomerAccount,row.account_id)

def is_verified(db, account_id):
    row=db.get(AccountEmailVerification,account_id)
    return bool(row and row.verified_at)

def authenticate(db,email,password):
    row=db.scalar(select(CustomerAccount).where(CustomerAccount.email==email.strip().lower(), CustomerAccount.active.is_(True)))
    return row if row and is_verified(db,row.id) and check_password_hash(row.password_hash,password) else None



def password_reset_token(db, account_id, minutes=60):
    raw=secrets.token_urlsafe(32); now=utcnow(); row=db.get(AccountPasswordReset, account_id)
    if row is None:
        row=AccountPasswordReset(account_id=account_id, token_hash=hash_key(raw), expires_at=now+timedelta(minutes=minutes), requested_at=now)
        db.add(row)
    else:
        row.token_hash=hash_key(raw); row.expires_at=now+timedelta(minutes=minutes); row.requested_at=now; row.used_at=None
    db.commit(); return raw

def reset_password(db, raw, new_password):
    row=db.scalar(select(AccountPasswordReset).where(AccountPasswordReset.token_hash==hash_key(raw)))
    if not row or row.used_at is not None: return None
    exp=row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if exp < utcnow(): return None
    account=db.get(CustomerAccount,row.account_id)
    if not account or not account.active: return None
    account.password_hash=generate_password_hash(new_password)
    row.used_at=utcnow(); db.commit(); return account

def manually_verify_account(db, account_id):
    now=utcnow(); row=db.get(AccountEmailVerification,account_id)
    if row is None:
        row=AccountEmailVerification(account_id=account_id, token_hash='admin-'+secrets.token_hex(24), expires_at=now, sent_at=now, verified_at=now)
        db.add(row)
    else:
        row.verified_at=now
    db.commit(); return db.get(CustomerAccount,account_id)

def create_connector(db, account_id, name, expires_at=None):
    raw=new_connector_key()
    row=Connector(account_id=account_id,name=name.strip(),key_hash=hash_key(raw),key_prefix=raw[:14],expires_at=expires_at)
    db.add(row); db.commit(); db.refresh(row); return row,raw

def rotate_connector(db,row):
    if getattr(row,'deleted_at',None) is not None:
        raise ValueError('Connector deleted')
    raw=new_connector_key(); row.key_hash=hash_key(raw); row.key_prefix=raw[:14]; row.rotated_at=utcnow(); db.commit(); return raw

def delete_connector(db,row):
    if getattr(row,'deleted_at',None) is not None:
        return row
    row.active=False
    row.deleted_at=utcnow()
    # Revoke the old key without deleting the row: JobBilling keeps its historical connector reference.
    raw='deleted_'+secrets.token_urlsafe(48)
    row.key_hash=hash_key(raw)
    row.key_prefix='smazáno'
    db.commit()
    return row

def find_connector(db, raw):
    now=utcnow()
    return db.scalar(select(Connector).where(
        Connector.key_hash==hash_key(raw),
        Connector.active.is_(True),
        Connector.deleted_at.is_(None),
        or_(Connector.expires_at.is_(None), Connector.expires_at>now),
    ))

def create_order(db, account_id, credits):
    credits=max(1.0,float(credits)); row=CreditOrder(account_id=account_id,credits=credits,amount_czk=round(credits*credit_price_czk(db),2))
    db.add(row); db.commit(); db.refresh(row); return row

def mark_order_paid(db,row,reference='admin'):
    if row.status=='paid': return row
    row.status='paid'; row.paid_at=utcnow(); row.payment_reference=reference
    db.add(CreditLedger(account_id=row.account_id,delta_credits=row.credits,entry_type='topup',reference=row.id,note='Dobití kreditů'))
    db.commit(); return row

def bind_job(db,job_id,connector,reserve_credits=0.0):
    if db.get(JobBilling,job_id): return
    reserve=max(0.0,round(float(reserve_credits or 0),6))
    if reserve>0 and balance(db,connector.account_id)+1e-9 < reserve: raise ValueError('Insufficient credits')
    db.add(JobBilling(job_id=job_id,account_id=connector.account_id,connector_id=connector.id,reserved_credits=reserve))
    if reserve>0: db.add(CreditLedger(account_id=connector.account_id,delta_credits=-reserve,entry_type='reserve',reference=job_id,note='Rezervace pro AI požadavek'))
    db.commit()

def settle_job(db,job):
    bill=db.get(JobBilling,job.id)
    if not bill: return
    reserve=float(bill.reserved_credits or 0)
    if bill.credits_charged>0 or (reserve==0 and job.status not in {'completed','failed','cancelled'}): return
    actual=round(float(job.estimated_cost_usd or 0)*credits_per_usd(db),6) if job.status=='completed' else 0.0
    bill.cost_usd=float(job.estimated_cost_usd or 0) if job.status=='completed' else 0.0
    bill.credits_charged=actual
    # Historical price snapshots must not be rewritten by subsequent admin tariff changes.
    if job.status == 'completed' and bill.credit_unit_price_czk is None:
        bill.credit_unit_price_czk = credit_price_czk(db)
        rate = credit_setting(db, 'usd_czk_reporting_rate', 0.0)
        bill.usd_czk_rate = rate if rate > 0 else None
        route = db.scalar(select(ModelRoute).where(ModelRoute.code == job.selected_route_code)) if job.selected_route_code else None
        if route:
            bill.input_price_usd_per_million = float(route.input_usd_per_million)
            bill.output_price_usd_per_million = float(route.output_usd_per_million)
    delta=round(reserve-actual,6)
    if delta!=0: db.add(CreditLedger(account_id=bill.account_id,delta_credits=delta,entry_type='settlement',reference=job.id,note='Vyrovnání rezervace AI požadavku'))
    db.commit()

def charge_job(db,job):
    settle_job(db,job)


def admin_suspended(db, account_id):
    from app.models import AccountAdminState
    row=db.get(AccountAdminState,account_id)
    return bool(row and row.suspended)


def make_invitation(db, account_id, hours=24):
    from app.models import AccountInvitation
    raw=secrets.token_urlsafe(36); now=utcnow()
    row=db.get(AccountInvitation,account_id)
    if row is None:
        row=AccountInvitation(account_id=account_id,token_hash=hash_key(raw),expires_at=now+timedelta(hours=hours))
        db.add(row)
    else:
        row.token_hash=hash_key(raw); row.expires_at=now+timedelta(hours=hours); row.used_at=None; row.created_at=now
    db.commit()
    return raw


def accept_invitation(db, token, password):
    from app.models import AccountInvitation, ExternalIdentity
    row=db.scalar(select(AccountInvitation).where(AccountInvitation.token_hash==hash_key(token)))
    if not row or row.used_at is not None: return None
    expiration=row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if expiration<utcnow(): return None
    account=db.get(CustomerAccount,row.account_id)
    if not account or not account.active or db.get(ExternalIdentity,account.id): return None
    account.password_hash=generate_password_hash(password)
    row.used_at=utcnow()
    # Verification and password creation take place in the same one-use e-mail link.
    from app.models import AccountEmailVerification
    verification=db.get(AccountEmailVerification,account.id)
    if verification:
        verification.verified_at=utcnow()
    else:
        db.add(AccountEmailVerification(account_id=account.id,token_hash='invited-'+secrets.token_hex(24),expires_at=utcnow(),verified_at=utcnow()))
    db.commit()
    return account
