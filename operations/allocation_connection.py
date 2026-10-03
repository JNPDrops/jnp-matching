"""Dedicated JNP Allocation connection for debtor routing and a quota probe.

The routing worker uses this app permanently. There is no key rotation or quota
fallback. Credentials stay in Render and tokens in their own existing-DB row.
Public status exposes only configuration flags and cached quota metadata.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx

from operations.bacs_debtor_transfer import TLS_CONTEXT

router = APIRouter()
BASE = 'https://start.exactonline.nl'
DIVISION = 3977752
CALLBACK = 'https://jnp-matching.onrender.com/oauth/allocation/callback'
LOCK_ID = 3977752100199
COOLDOWN = 60
_lock = asyncio.Lock()


def main_module():
    from app import main
    return main


def configuration():
    return {
        'client_id': os.getenv('EXACT_ALLOCATION_CLIENT_ID', '').strip(),
        'client_secret': os.getenv('EXACT_ALLOCATION_CLIENT_SECRET', '').strip(),
        'redirect_uri': os.getenv('EXACT_ALLOCATION_REDIRECT_URI', CALLBACK).strip(),
    }


def configured(config):
    return bool(config['client_id'] and config['client_secret'] and config['redirect_uri'] == CALLBACK)


def require_configuration():
    config, app = configuration(), main_module()
    if not configured(config):
        raise HTTPException(503, 'Stel EXACT_ALLOCATION_CLIENT_ID en EXACT_ALLOCATION_CLIENT_SECRET in Render in; controleer de Redirect URI.')
    if not app.DATABASE_URL:
        raise HTTPException(503, 'Bestaande permanente tokenopslag is niet beschikbaar.')
    if config['client_id'] == app.CLIENT_ID:
        raise HTTPException(409, 'JNP Allocation moet een eigen Client ID hebben.')
    if app.SESSION_SECRET == 'dev-only-change-me':
        raise HTTPException(503, 'Een eigen sessiesleutel is vereist voor autorisatie.')
    return config


def store_key(config):
    # Changing an app ID can never reuse another app's rotating refresh token.
    return 'exact_allocation:' + hashlib.sha256(config['client_id'].encode()).hexdigest()


def load_tokens(conn, config):
    row = conn.execute('SELECT token_json FROM exact_oauth_tokens WHERE singleton_key=%s',
                       (store_key(config),)).fetchone()
    return row[0] if row else None


def save_tokens(conn, config, tokens):
    conn.execute('''INSERT INTO exact_oauth_tokens(singleton_key,token_json,updated_at)
        VALUES(%s,%s::jsonb,NOW()) ON CONFLICT(singleton_key)
        DO UPDATE SET token_json=EXCLUDED.token_json,updated_at=NOW()''',
        (store_key(config),json.dumps(tokens)))


async def exchange(config, grant, previous=None):
    try:
        async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
            response = await client.post(BASE + '/api/oauth2/token', data={**grant,
                'client_id':config['client_id'],'client_secret':config['client_secret']})
        if response.status_code != 200:
            raise HTTPException(502, 'Exact-tokenuitwisseling afgewezen; verbind opnieuw. Details afgeschermd.')
        raw = response.json()
        if not isinstance(raw,dict) or not isinstance(raw.get('access_token'),str) or not raw['access_token']:
            raise ValueError()
        refresh = raw.get('refresh_token') or (previous or {}).get('refresh_token')
        if not isinstance(refresh,str) or not refresh: raise ValueError()
        # Store only fields required for this connection. Never return them.
        return {'access_token':raw['access_token'],'refresh_token':refresh,
                'expires_at':int(time.time()) + int(raw.get('expires_in',600)) - 30,
                'probe':(previous or {}).get('probe')}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'Exact-tokenuitwisseling mislukt; details afgeschermd.') from None


async def routing_access_token(app):
    """Use this app only; serialize rotating tokens with the probe and callback."""
    config=require_configuration()
    async with _lock:
        with app._db_connect() as conn:
            if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0]:
                raise HTTPException(409,'JNP Allocation-autorisatie wordt bijgewerkt; probeer later.')
            try:
                tokens=load_tokens(conn,config)
                if not tokens:
                    raise HTTPException(401,'JNP Allocation is niet verbonden.')
                if int(tokens.get('expires_at',0))<=time.time():
                    tokens=await exchange(config,{'grant_type':'refresh_token','refresh_token':tokens['refresh_token']},tokens)
                    save_tokens(conn,config,tokens)
                if not isinstance(tokens.get('access_token'),str) or not tokens['access_token']:
                    raise HTTPException(401,'JNP Allocation-token ontbreekt.')
                return tokens['access_token']
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))


class RoutingApp:
    """Narrow facade: preserve target division while selecting the dedicated app."""
    def __init__(self, app):
        self.original=app
        self.DIVISION=app.DIVISION
        self.BASE_URL=app.BASE_URL
        self.COLLECTIVE_DEBTOR_CODE=app.COLLECTIVE_DEBTOR_CODE

    async def _access_token(self):
        return await routing_access_token(self.original)


def quota_result(response):
    headers = response.headers
    result={'checked_at':datetime.now(timezone.utc).isoformat(),
            'http_status':response.status_code,'division':DIVISION,
            'read_succeeded':response.status_code == 200,
            'rate_limited':response.status_code == 429}
    for name,header in (
        ('daily_limit','x-ratelimit-limit'),('daily_remaining','x-ratelimit-remaining'),
        ('daily_reset_ms','x-ratelimit-reset'),('minute_limit','x-ratelimit-minutely-limit'),
        ('minute_remaining','x-ratelimit-minutely-remaining'),('minute_reset_ms','x-ratelimit-minutely-reset')):
        value=headers.get(header,'')
        result[name]=int(value) if value.isdigit() else None
    # These are this app's reported headers, not an estimate of the remaining
    # shared administration-wide allowance. A missing header stays unknown.
    result['administration_remaining']=None
    return result


async def probe(conn, config, tokens):
    previous=tokens.get('probe')
    if previous:
        elapsed=time.time()-datetime.fromisoformat(previous['checked_at']).timestamp()
        if elapsed<COOLDOWN: return previous
    if int(tokens.get('expires_at',0))<=time.time():
        tokens=await exchange(config,{'grant_type':'refresh_token','refresh_token':tokens['refresh_token']},tokens)
        # Persist each rotating token before doing any further request.
        save_tokens(conn,config,tokens)
    try:
        async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
            response=await client.get(BASE+f'/api/v1/{DIVISION}/crm/Accounts',
                params={'$select':'ID','$filter':"Code eq '            100100'",'$top':1},
                headers={'Authorization':'Bearer '+tokens['access_token'],'Accept':'application/json'})
        result=quota_result(response)
        # No response body, account data, request URLs or credentials are kept.
    except Exception:
        result={'checked_at':datetime.now(timezone.utc).isoformat(),'division':DIVISION,
                'read_succeeded':False,'error':'Exact-leesaanvraag mislukt; details afgeschermd.'}
    tokens['probe']=result
    save_tokens(conn,config,tokens)
    return result


@router.get('/allocation/status')
async def status():
    config=configuration()
    result={'connection':'JNP Allocation','configured':configured(config),'connected':False,
            'division':DIVISION,'read_only':False,'usage':'customer_only_debtor_routing',
            'probe_read_only':True,'automatic_key_switching':False,'last_probe':None}
    from operations.automatic_debtor_routing import STATUS
    result['routing']={key:STATUS.get(key) for key in
        ('state','current_phase','api_limits','api_limits_checked_at','applied_since_start')}
    app=main_module()
    if not config['client_id'] or not app.DATABASE_URL: return result
    try:
        with app._db_connect() as conn:
            # Select metadata only: public status never loads an access/refresh token.
            row=conn.execute("SELECT token_json->'probe' FROM exact_oauth_tokens WHERE singleton_key=%s",
                             (store_key(config),)).fetchone()
        if row: result.update(connected=True,last_probe=row[0])
    except Exception:
        result['storage_available']=False
    return result


@router.get('/allocation',response_class=HTMLResponse)
async def page(request:Request):
    allowed=request.session.get('allocation_operator')==store_key(configuration())
    button=''
    if allowed:
        csrf=request.session.get('allocation_probe_csrf','')
        button=f'<form method="post" action="/allocation/probe"><input type="hidden" name="csrf" value="{csrf}"><button>API-ruimte opnieuw controleren</button></form>'
    return HTMLResponse('<!doctype html><html lang="nl"><meta charset="utf-8"><title>JNP Allocation</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><body style="font:17px system-ui;max-width:760px;margin:48px auto;padding:0 20px">'
        '<h1>JNP Allocation</h1><p>Exact-koppeling voor debiteurenomzetting en controle van de beschikbare API-ruimte.</p>'
        '<p><a href="/allocation/login">Verbinden met Exact Online</a></p>'+button+
        '<p><a href="/allocation/status">Bekijk het laatste resultaat</a></p>'
        '<p>De leesproef wijzigt geen boekingen. De debiteurenagent gebruikt deze aansluiting voor de afgesproken debiteurwijzigingen. De teller betreft deze app; de gedeelde administratielimiet kan daarnaast gelden.</p></body></html>',
        headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


@router.get('/allocation/login')
async def login(request:Request):
    config=require_configuration()
    state=secrets.token_urlsafe(32)
    request.session['allocation_oauth_state']={'state':state,'created':int(time.time()),'key':store_key(config)}
    query=urlencode({'client_id':config['client_id'],'redirect_uri':config['redirect_uri'],
                     'response_type':'code','state':state})
    return RedirectResponse(BASE+'/api/oauth2/auth?'+query,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


@router.get('/oauth/allocation/callback')
async def callback(request:Request,code:str|None=None,state:str|None=None,error:str|None=None):
    # FastAPI has parsed the query. Suppress the one-use authorization code from
    # Uvicorn's access-log URL before a response is sent.
    request.scope['query_string']=b''
    config=require_configuration()
    pending=request.session.pop('allocation_oauth_state',None)
    if (error or not code or not state or not isinstance(pending,dict)
            or not hmac.compare_digest(state,pending.get('state',''))
            or pending.get('key')!=store_key(config)
            or not 0<=time.time()-pending.get('created',0)<=600):
        raise HTTPException(400,'Exact-autorisatie ongeldig of verlopen. Start opnieuw via /allocation/login.')
    app=main_module()
    async with _lock:
        with app._db_connect() as conn:
            app._ensure_token_table(conn)
            if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0]:
                raise HTTPException(409,'Deze koppeling wordt al bijgewerkt; probeer opnieuw.')
            try:
                tokens=await exchange(config,{'grant_type':'authorization_code','code':code,'redirect_uri':config['redirect_uri']})
                save_tokens(conn,config,tokens)
                await probe(conn,config,tokens)
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))
    request.session['allocation_operator']=store_key(config)
    request.session['allocation_probe_csrf']=secrets.token_urlsafe(32)
    request.session['allocation_authorized_at']=int(time.time())
    return RedirectResponse('/allocation/status',status_code=303,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


@router.post('/allocation/probe')
async def run_probe(request:Request):
    config=require_configuration()
    from urllib.parse import parse_qs
    # No multipart dependency and no token/credential form fields.
    body=await request.body()
    if len(body)>512: raise HTTPException(400,'Ongeldig verzoek.')
    csrf=parse_qs(body.decode('ascii',errors='ignore')).get('csrf',[''])[0]
    if (request.session.get('allocation_operator')!=store_key(config)
            or time.time()-request.session.get('allocation_authorized_at',0)>3600
            or not csrf or not hmac.compare_digest(csrf,request.session.get('allocation_probe_csrf',''))):
        raise HTTPException(403,'Autoriseer JNP Allocation eerst via /allocation/login.')
    app=main_module()
    async with _lock:
        with app._db_connect() as conn:
            if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0]:
                raise HTTPException(409,'Een leesproef is al bezig.')
            try:
                tokens=load_tokens(conn,config)
                if not tokens: raise HTTPException(401,'Verbind JNP Allocation opnieuw.')
                await probe(conn,config,tokens)
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))
    return RedirectResponse('/allocation/status',status_code=303,headers={'Cache-Control':'no-store'})
