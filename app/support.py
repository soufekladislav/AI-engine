"""Customer support: CSRF, arithmetic challenge, honeypot, persistent throttling,
optional Cloudflare Turnstile and SMTP. Never store message bodies in the DB.
"""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import urlsplit

import httpx
from flask import request, session
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import SupportSubmission
from app.mailer import send_email

SUPPORT_EMAIL = 'soufek@freshcrackers.cz'


def create_challenge():
    if not session.get('support_challenge') or time.time() - float(session.get('support_challenge_at', 0)) > 900:
        a = secrets.randbelow(9) + 2
        b = secrets.randbelow(9) + 1
        session['support_challenge'] = [a, b]
        session['support_challenge_at'] = time.time()
        session['support_csrf'] = secrets.token_urlsafe(28)
    a, b = session['support_challenge']
    return {'question': f'{a} + {b} = ?', 'csrf': session['support_csrf'],
            'turnstile_site_key': os.getenv('SUPPORT_TURNSTILE_SITE_KEY', '').strip()}


def _canonical_email(raw: str) -> str:
    email = raw.strip().lower()
    if (len(email) > 190 or '\n' in email or '\r' in email or
            parseaddr(email)[1] != email or email.count('@') != 1 or '.' not in email.rsplit('@', 1)[1]):
        raise ValueError('Zadej platnou e-mailovou adresu.')
    return email


def _verify_turnstile(token: str) -> bool:
    secret = os.getenv('SUPPORT_TURNSTILE_SECRET_KEY', '').strip()
    if not secret:
        return not bool(os.getenv('SUPPORT_TURNSTILE_SITE_KEY', '').strip())
    if not token:
        return False
    try:
        response = httpx.post('https://challenges.cloudflare.com/turnstile/v0/siteverify',
                              data={'secret': secret, 'response': token}, timeout=7)
        response.raise_for_status()
        return response.json().get('success') is True
    except (httpx.HTTPError, ValueError):
        return False


def submit_support(db: Session, form, account_id: str | None, client_ip: str) -> str:
    # The contact form is deliberately available to guests, including login pages.
    csrf = session.get('support_csrf', '')
    if not csrf or not secrets.compare_digest(form.get('support_csrf', ''), csrf):
        raise ValueError('Formulář vypršel. Obnov stránku a odešli zprávu znovu.')
    if form.get('support_site', '').strip():
        raise ValueError('Zprávu se nepodařilo odeslat.')
    if time.time() - float(session.get('support_challenge_at', 0)) < 2:
        raise ValueError('Zkus formulář odeslat znovu za okamžik.')
    challenge = session.get('support_challenge')
    try:
        correct = isinstance(challenge, list) and len(challenge) == 2 and int(form.get('support_answer', '')) == sum(challenge)
    except (ValueError, TypeError):
        correct = False
    if not correct or not _verify_turnstile(form.get('cf-turnstile-response', '')):
        raise ValueError('Ověření proti robotům nebylo úspěšné. Zkus to znovu.')

    email = _canonical_email(form.get('support_email', ''))
    name = form.get('support_name', '').strip()[:100]
    topic = form.get('support_topic', '').strip()[:120]
    message = form.get('support_message', '').strip()
    if not topic or len(message) < 10 or len(message) > 4000:
        raise ValueError('Vyplň téma a zprávu o délce 10 až 4 000 znaků.')
    if not form.get('support_privacy'):
        raise ValueError('Potvrď prosím seznámení se zásadami ochrany osobních údajů.')

    # Stable privacy-preserving IP/email hashes, no original IP/message in DB.
    key = os.getenv('SUPPORT_RATE_SECRET') or os.getenv('CUSTOMER_SESSION_SECRET', '')
    if len(key) < 24:
        raise RuntimeError('Pro bezpečné odesílání podpory nastav CUSTOMER_SESSION_SECRET.')
    ip_hash = hmac.new(key.encode(), ('ip:' + client_ip).encode(), hashlib.sha256).hexdigest()
    email_hash = hmac.new(key.encode(), ('email:' + email).encode(), hashlib.sha256).hexdigest()
    now = datetime.now(timezone.utc)
    # Compatible with DB backends returning naive UTC datetime values.
    last15 = now - timedelta(minutes=15)
    last60 = now - timedelta(hours=1)
    by_ip = db.scalar(select(func.count(SupportSubmission.id)).where(
        SupportSubmission.ip_hash == ip_hash, SupportSubmission.created_at >= last15)) or 0
    by_email = db.scalar(select(func.count(SupportSubmission.id)).where(
        SupportSubmission.email_hash == email_hash, SupportSubmission.created_at >= last60)) or 0
    if by_ip >= 4 or by_email >= 3:
        raise ValueError('Bylo odesláno příliš mnoho zpráv. Zkus to prosím později.')

    # One-use form token, do not send mail twice on repeated submissions.
    session.pop('support_csrf', None)
    session.pop('support_challenge', None)
    session.pop('support_challenge_at', None)
    reference = 'FAE-' + secrets.token_hex(5).upper()
    body = ('Nová zpráva technické podpoře Fresh AI Engine\n'
            f'Reference: {reference}\nJméno: {name}\nKontakt: {email}\n'
            f'Téma: {topic}\nÚčet: {account_id or "nepřihlášený"}\n\n'
            f'Zpráva:\n{message}\n')
    # Recipient and subject are fixed; untrusted user input is not an SMTP header.
    send_email(db, SUPPORT_EMAIL, f'Fresh AI Engine – podpora {reference}', body)
    db.add(SupportSubmission(ip_hash=ip_hash, email_hash=email_hash,
                             account_id=account_id, created_at=now, reference=reference))
    db.commit()
    return reference
