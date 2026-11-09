from __future__ import annotations
import json, os, urllib.request, urllib.parse, urllib.error

class FreshAccountError(RuntimeError):
    def __init__(self, message, status=500, code='error', payload=None):
        super().__init__(message); self.status=status; self.code=code; self.payload=payload or {}

def base_url(): return os.getenv('FRESH_ACCOUNT_URL','https://www.freshcrackers.cz').rstrip('/')
def app_id(): return os.getenv('FRESH_ACCOUNT_APP_ID','ai-engine').strip()
def secret(): return os.getenv('FRESH_ACCOUNT_SECRET','').strip()
def configured(): return bool(base_url() and app_id() and secret())

def _request(path, method='GET', data=None, query=None, timeout=15):
    url=base_url()+'/wp-json/fresh-account/v1'+path
    if query: url += '?' + urllib.parse.urlencode(query)
    body=None
    headers={'Accept':'application/json','X-Fresh-App-Id':app_id(),'X-Fresh-App-Secret':secret()}
    if data is not None:
        body=json.dumps(data,ensure_ascii=False).encode('utf-8'); headers['Content-Type']='application/json'
    req=urllib.request.Request(url,data=body,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as res:
            raw=res.read().decode('utf-8','replace')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw=exc.read().decode('utf-8','replace')
        try: payload=json.loads(raw)
        except Exception: payload={'message':raw[:300]}
        msg=payload.get('message') or payload.get('detail') or f'Fresh Account HTTP {exc.code}'
        raise FreshAccountError(msg,exc.code,payload.get('code','remote_error'),payload)
    except Exception as exc:
        raise FreshAccountError(f'Fresh Account není dostupný: {type(exc).__name__}: {exc}',503,'unavailable')

def password_login(email,password): return _request('/auth/password','POST',{'email':email,'password':password})
def register(email,password,privacy_ack,return_url): return _request('/account/register','POST',{'email':email,'password':password,'privacy_ack':bool(privacy_ack),'return_url':return_url})
def resend_verification(email,return_url): return _request('/account/verification/resend','POST',{'email':email,'return_url':return_url})
def password_forgot(email): return _request('/account/password/forgot','POST',{'email':email})
def exchange_code(code): return _request('/auth/exchange','POST',{'code':code})
def orders(external_user_id): return _request('/orders','GET',query={'user_id':external_user_id})
def profile(external_user_id): return _request('/account/profile','GET',query={'user_id':external_user_id})
def update_profile(external_user_id, payload):
    data=dict(payload or {}); data['user_id']=external_user_id
    return _request('/account/profile','POST',data)
def checkout(external_user_id, credits, product_code='default'): return _request('/checkout','POST',{'user_id':external_user_id,'product_code':product_code,'credits':int(credits)})
def sso_start_url(return_url,state):
    q=urllib.parse.urlencode({'app_id':app_id(),'return_url':return_url,'state':state})
    # Browser-facing WordPress endpoint: REST cookie authentication without WP nonce
    # intentionally treats the user as logged out, even with a valid Woo session.
    return base_url()+'/?fac_sso_start=1&'+q


def admin_orders(page=1): return _request('/admin/orders','GET',query={'page':int(page)})
