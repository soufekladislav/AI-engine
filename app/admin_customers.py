"""Account administration. Never treat an internal credit order as a WooCommerce payment."""
from datetime import datetime, timezone
from sqlalchemy import select, func
from app.models import (CustomerAccount, AccountTrash, AccountInvitation,
    AccountPasswordReset, AccountEmailVerification, AccountAdminState,
    AdminAccountAudit, Connector, JobBilling, Job, CreditLedger, ExternalIdentity,
    SupportSubmission)
from app.accounts import balance, is_verified


def utcnow(): return datetime.now(timezone.utc)


def audit(db, account_id, action, detail=''):
    db.add(AdminAccountAudit(account_id=account_id,action=action,details=str(detail)[:255]))


def initialize_trash(db):
    """Reconcile legacy 0.4.x deleted accounts without rewriting billing records."""
    for a in db.scalars(select(CustomerAccount).where(CustomerAccount.active.is_(False))).all():
        if db.get(AccountTrash,a.id) is None:
            db.add(AccountTrash(account_id=a.id,deleted_at=a.updated_at or utcnow()))
    db.commit()


def trash_account(db,account):
    """Anonymize local PII, revoke access, preserve financial/job references."""
    if not account.active: return
    remaining=balance(db,account.id)
    if remaining>0:
        db.add(CreditLedger(account_id=account.id,delta_credits=-remaining,
            entry_type='account_deletion',reference=account.id,note='Ukončení účtu'))
    for c in db.scalars(select(Connector).where(Connector.account_id==account.id)).all():
        c.active=False
        if getattr(c,'deleted_at',None) is None: c.deleted_at=utcnow()
        # Keep connector ID for historical JobBilling foreign-key references.
        c.name='Uzavřený konektor'
        from app.accounts import hash_key
        import secrets
        c.key_hash=hash_key(secrets.token_urlsafe(48))
        c.key_prefix='zrušeno'
    identity=db.get(ExternalIdentity,account.id)
    if identity: db.delete(identity)
    account.email='deleted-'+account.id+'@invalid.local'
    account.password_hash='deleted'
    account.company_name=account.billing_name=account.billing_ico=account.billing_dic=account.billing_address=''
    account.active=False
    for cls in (AccountInvitation,AccountPasswordReset,AccountEmailVerification):
        row=db.get(cls,account.id)
        if row: db.delete(row)
    if db.get(AccountTrash,account.id) is None:
        db.add(AccountTrash(account_id=account.id,deleted_at=utcnow()))
    audit(db,account.id,'delete','Účet anonymizován a přístup zneplatněn')
    db.commit()


def purge_trash(db):
    """Remove residual personal metadata; preserve pseudonymous financial tombstones."""
    rows=db.scalars(select(AccountTrash).where(AccountTrash.purged_at.is_(None))).all()
    for row in rows:
        account=db.get(CustomerAccount,row.account_id)
        if account and not account.active:
            account.email='deleted-'+account.id+'@invalid.local'
            account.password_hash='deleted'
            account.company_name=account.billing_name=account.billing_ico=account.billing_dic=account.billing_address=''
            for cls in (AccountInvitation,AccountPasswordReset,AccountEmailVerification):
                token=db.get(cls,account.id)
                if token: db.delete(token)
            for submission in db.scalars(select(SupportSubmission).where(SupportSubmission.account_id==account.id)).all():
                submission.account_id=None
            for c in db.scalars(select(Connector).where(Connector.account_id==account.id)).all():
                c.active=False; c.name='Uzavřený konektor'; c.key_prefix='zrušeno'
                if getattr(c,'deleted_at',None) is None: c.deleted_at=utcnow()
        row.purged_at=utcnow()
        audit(db,row.account_id,'trash_purged','Osobní metadata odstraněna; finanční tombstone zachován')
    db.commit()
    return len(rows)


def projects_for_account(db, account_id):
    rows=db.execute(
        select(Job.project_id,Job.source,Connector.name,
               func.count(Job.id),func.coalesce(func.sum(JobBilling.credits_charged),0),
               func.coalesce(func.sum(JobBilling.cost_usd),0))
        .select_from(JobBilling)
        .join(Job,Job.id==JobBilling.job_id)
        .outerjoin(Connector,Connector.id==JobBilling.connector_id)
        .where(JobBilling.account_id==account_id)
        .group_by(Job.project_id,Job.source,Connector.name)
        .order_by(func.count(Job.id).desc())
    ).all()
    return [{'project':p or 'Bez označení projektu','source':src or '—',
             'connector':name or '—','jobs':count,'credits':float(credits or 0),
             'cost_usd':float(cost or 0)} for p,src,name,count,credits,cost in rows]
