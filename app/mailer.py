from __future__ import annotations
import os, smtplib, ssl
from email.message import EmailMessage
from pathlib import Path
from app.models import RuntimeSetting

ENV_PATH = Path(__file__).resolve().parent.parent / '.env'

def setting(db, key, default=''):
    row=db.get(RuntimeSetting,key)
    return row.value if row else default

def smtp_config(db):
    return {
        'host': setting(db,'smtp_host',''), 'port': int(setting(db,'smtp_port','587') or 587),
        'user': setting(db,'smtp_user',''), 'password': os.getenv('SMTP_PASSWORD',''),
        'password_present': bool(os.getenv('SMTP_PASSWORD','')),
        'from_email': setting(db,'smtp_from_email',''), 'from_name': setting(db,'smtp_from_name','Fresh AI Engine'),
        'security': setting(db,'smtp_security','starttls'), 'enabled': setting(db,'smtp_enabled','false')=='true'
    }

def save_env_secret(name, value):
    lines=ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out=[]; found=False
    for line in lines:
        if line.startswith(name+'='):
            out.append(f'{name}=' + __import__('json').dumps(value)); found=True
        else: out.append(line)
    if not found: out.append(f'{name}=' + __import__('json').dumps(value))
    ENV_PATH.write_text('\n'.join(out)+'\n')
    os.environ[name]=value

def send_email(db,to_email,subject,text,html=None):
    cfg=smtp_config(db)
    if not cfg['enabled'] or not cfg['host'] or not cfg['from_email']:
        raise RuntimeError('SMTP není nakonfigurováno nebo aktivováno.')
    if cfg['user'] and not cfg['password']:
        raise RuntimeError('Není uložené heslo SMTP schránky. Vyplň heslo a nastavení ulož.')
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=f"{cfg['from_name']} <{cfg['from_email']}>"; msg['To']=to_email
    msg.set_content(text)
    if html: msg.add_alternative(html,subtype='html')
    if cfg['security']=='ssl':
        smtp=smtplib.SMTP_SSL(cfg['host'],cfg['port'],timeout=20,context=ssl.create_default_context())
    else:
        smtp=smtplib.SMTP(cfg['host'],cfg['port'],timeout=20)
        smtp.ehlo()
        if cfg['security']=='starttls': smtp.starttls(context=ssl.create_default_context()); smtp.ehlo()
    try:
        if cfg['user']: smtp.login(cfg['user'],cfg['password'])
        smtp.send_message(msg)
    finally: smtp.quit()
