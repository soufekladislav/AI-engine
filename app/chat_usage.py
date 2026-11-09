"""Admin-only per-user Fresh Chat spend. Unknown history stays unassigned.
Unbilled Chat requests are never presented as credits sold or profit.
"""
from datetime import datetime, timezone
from sqlalchemy import select, func
from app.models import Job, JobBilling, ChatUsageUser, RuntimeSetting
from app.accounts import credit_price_czk, credit_setting


def accept_chat_identity(db, payload):
    identity=payload.context.pop('chat_identity', None)
    if not isinstance(identity,dict):
        payload.user_id=None
        return None
    uid=str(identity.get('chat_user_id') or '')
    fresh=str(identity.get('fresh_user_id') or '')
    if not uid.isdecimal() or len(uid)>20 or not fresh.isdecimal() or len(fresh)>40:
        payload.user_id=None
        return None
    key='chat:'+uid
    payload.user_id=key
    row=db.get(ChatUsageUser,key)
    if row is not None and row.fresh_user_id != fresh:
        # Never silently reassign an existing user ID to another central identity.
        payload.user_id=None
        return None
    if row is None:
        row=ChatUsageUser(chat_user_id=key, fresh_user_id=fresh)
        db.add(row)
    row.display_name=str(identity.get('display_name') or '')[:160]
    row.email=str(identity.get('email') or '')[:190]
    row.last_seen_at=datetime.now(timezone.utc)
    db.flush()
    return row


def chat_user_summary(db, key=None):
    price=credit_price_czk(db)
    fx=credit_setting(db, 'usd_czk_reporting_rate', 0.0)
    stmt=(select(Job,JobBilling)
        .outerjoin(JobBilling, JobBilling.job_id==Job.id)
        .where(func.lower(Job.source).in_(['fresh-chat','fresh chat']), Job.status=='completed'))
    if key is not None:
        stmt=stmt.where(Job.user_id==key)
    rows=db.execute(stmt.order_by(Job.created_at.desc())).all()
    groups={}
    for job,bill in rows:
        k=job.user_id if job.user_id and job.user_id.startswith('chat:') and job.user_id[5:].isdecimal() else 'legacy'
        r=groups.setdefault(k,dict(id=k,jobs=0,input_tokens=0,output_tokens=0,
            credits=0.0,nominal_czk=0.0,cost_usd=0.0,cost_czk=0.0,
            unbilled=0,missing_fx=False,models={},projects={},recent=[]))
        r['jobs']+=1
        r['input_tokens']+=int(job.input_tokens or 0)
        r['output_tokens']+=int(job.output_tokens or 0)
        cost=float(job.estimated_cost_usd or 0)
        r['cost_usd']+=cost
        rate=(float(bill.usd_czk_rate) if bill and bill.usd_czk_rate else fx)
        r['cost_czk']+=cost*rate if rate>0 else 0
        if cost>0 and rate<=0: r['missing_fx']=True
        if bill:
            charged=max(0.,float(bill.credits_charged or 0))
            r['credits']+=charged
            r['nominal_czk']+=charged*(float(bill.credit_unit_price_czk) if bill.credit_unit_price_czk is not None else price)
        else:
            r['unbilled']+=1
        model=(job.provider or '—',job.model_name or '—')
        mg=r['models'].setdefault(model,dict(provider=model[0],model=model[1],jobs=0,cost_usd=0.0,input_tokens=0,output_tokens=0))
        mg['jobs']+=1;mg['cost_usd']+=cost;mg['input_tokens']+=int(job.input_tokens or 0);mg['output_tokens']+=int(job.output_tokens or 0)
        project=job.project_id or 'Bez projektu'
        pg=r['projects'].setdefault(project,dict(name=project,jobs=0,cost_usd=0.0))
        pg['jobs']+=1;pg['cost_usd']+=cost
        if len(r['recent'])<30:r['recent'].append(job)
    directory={row.chat_user_id:row for row in db.scalars(select(ChatUsageUser)).all()}
    for k,r in groups.items():
        r['identity']=directory.get(k)
        r['models']=list(r['models'].values())
        r['projects']=list(r['projects'].values())
        # Do not manufacture a margin from unbilled calls.
        r['margin_czk']=r['nominal_czk']-r['cost_czk'] if not r['unbilled'] and not r['missing_fx'] else None
        r['margin_pct']=100*r['margin_czk']/r['nominal_czk'] if r['margin_czk'] is not None and r['nominal_czk']>0 else None
    # Directory may include users with no external calls (local-only answer).
    for k,identity in directory.items():
        if k not in groups and key is None:
            groups[k]=dict(id=k,identity=identity,jobs=0,input_tokens=0,output_tokens=0,
                credits=0.0,nominal_czk=0.0,cost_usd=0.0,cost_czk=0.0,unbilled=0,
                missing_fx=False,models=[],projects=[],recent=[],margin_czk=None,margin_pct=None)
    return sorted(groups.values(), key=lambda x:(-x['cost_usd'],x['id']))


def sync_chat_users(db):
    """Best-effort admin directory refresh, 15 min cache, authenticated over HTTPS.
    Database remains source of spend; Chat is source of displayed active identities.
    """
    import os
    import httpx
    from datetime import timedelta
    from app.config import get_settings
    from app.models import RuntimeSetting
    from urllib.parse import urlsplit
    now=datetime.now(timezone.utc)
    st=db.get(RuntimeSetting,'chat_directory_last_check')
    if st:
        try:
            last=datetime.fromisoformat(st.value)
            if last.tzinfo is None: last=last.replace(tzinfo=timezone.utc)
            if now-last<timedelta(minutes=15):
                return {'state':'cached'}
        except ValueError: pass
    if st is None:
        st=RuntimeSetting(key='chat_directory_last_check',value=now.isoformat());db.add(st)
    else:st.value=now.isoformat()
    db.commit()
    secret=get_settings().fresh_chat_reporting_secret
    root=os.getenv('FRESH_CHAT_URL','https://chat.freshcrackers.cz').rstrip('/')
    url=urlsplit(root)
    if not secret or len(secret)<32 or url.scheme!='https' or not url.hostname:
        return {'state':'not_configured'}
    try:
        with httpx.Client(timeout=httpx.Timeout(4.0,connect=2.0),follow_redirects=False) as client:
            response=client.get(root+'/internal/engine/users',headers={'Authorization':'Bearer '+secret})
            response.raise_for_status()
            data=response.json()
        if data.get('ok') is not True or not isinstance(data.get('users'),list):
            raise ValueError('Invalid Chat directory response')
        updated=0
        for identity in data['users'][:5000]:
            # Trusted endpoint, still validate row shape, avoid reassignment.
            key=str(identity.get('chat_user_id') or '')
            fresh=str(identity.get('fresh_user_id') or '')
            if not key.isdecimal() or len(key)>20 or not fresh.isdecimal() or len(fresh)>40:
                continue
            ck='chat:'+key
            row=db.get(ChatUsageUser,ck)
            if row is not None and row.fresh_user_id != fresh:
                continue
            if row is None:
                row=ChatUsageUser(chat_user_id=ck,fresh_user_id=fresh);db.add(row)
            row.email=str(identity.get('email') or '')[:190]
            row.display_name=str(identity.get('display_name') or '')[:160]
            row.last_seen_at=now
            updated+=1
        db.commit()
        return {'state':'ok','count':updated}
    except (httpx.HTTPError,ValueError,TypeError,KeyError):
        db.rollback()
        return {'state':'unavailable'}
