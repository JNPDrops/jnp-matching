"""Authenticated WooCommerce receipts -> Exact allocation rules for debtor 109372.

No invoice changes, bank reimports, matching, or deletion of existing rules.
The caller is the trusted single-store plugin; only authenticated evidence is queued.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from operations.bacs_debtor_transfer import TLS_CONTEXT

router = APIRouter()
CUTOFF = '2026-10-01'
ADMINISTRATION = '499917175367140629'
BANK_ACCOUNT = '499917633883211315'
DIVISION = 3977752
DEBTOR = '109372'
LOCK = 3977752109372
PATH = '/api/woocommerce/iban-rule'
REFERENCE_PATH = '/api/woocommerce/allocation-rule'
BASE = 'https://start.exactonline.nl'
STATUS = {'state': 'starting'}


def iban(value):
    if not isinstance(value, str): raise ValueError('IBAN ontbreekt')
    value = re.sub(r'\s+', '', value).upper()
    if not re.fullmatch(r'[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}', value): raise ValueError('IBAN ongeldig')
    digits = ''.join(str(ord(c)-55) if c.isalpha() else c for c in value[4:]+value[:4])
    if int(digits) % 97 != 1: raise ValueError('IBAN controlegetal ongeldig')
    return value


def camt_words(reference):
    # GoDutch CAMT AcctSvcrRef truncates the full 38-character Moneybird code to 35.
    if not isinstance(reference, str) or not re.fullmatch(r'bosci_[a-f0-9]{32}', reference):
        raise ValueError('Geen volledige eenduidige bosci-code')
    return reference[:35]


def validate(data, reference_mode=False):
    if not isinstance(data, dict): raise ValueError('Ongeldig bericht')
    required = {'transaction_id','administration_id','financial_account_id','order_id','order_number',
                'order_date','payment_date','amount','currency','payment_method','order_status','origin'}
    required.add('bank_reference' if reference_mode else 'iban')
    if set(data) != required: raise ValueError('Ontbrekende of onverwachte velden')
    if data['administration_id'] != ADMINISTRATION or data['financial_account_id'] != BANK_ACCOUNT: raise ValueError('Verkeerde Moneybird-rekening')
    if data['currency'] != 'EUR' or data['payment_method'] != 'bacs' or data['order_status'] not in ('completed','processing'): raise ValueError('Geen betaalde bankorder')
    if data['origin'] not in ('plugin_completed', 'historical_paid_exact', 'historical_paid_confirmed'): raise ValueError('Ongeldige herkomst')
    if not isinstance(data['transaction_id'], str) or not re.fullmatch(r'\d{1,24}', data['transaction_id']): raise ValueError('Transactie-ID ongeldig')
    if type(data['order_id']) is not int or data['order_id'] <= 0: raise ValueError('Order-ID ongeldig')
    if not isinstance(data['order_number'], str) or not re.fullmatch(r'[A-Za-z0-9#_-]{1,80}', data['order_number']): raise ValueError('Ordernummer ongeldig')
    if not isinstance(data['amount'], str) or not re.fullmatch(r'\d{1,10}\.\d{2}', data['amount']) or int(data['amount'].replace('.','')) <= 0: raise ValueError('Bedrag ongeldig')
    for field in ('order_date','payment_date'):
        if not isinstance(data[field], str) or date.fromisoformat(data[field]).isoformat() != data[field] or data[field] > date.today().isoformat(): raise ValueError('Ongeldige datum')
    if data['payment_date'] < CUTOFF: raise ValueError('Betaling voor 1 oktober 2026')
    if data['order_date'] < CUTOFF and (not reference_mode or data['origin'] != 'plugin_completed'): raise ValueError('Order voor 1 oktober 2026 zonder modulebevestiging')
    if data['payment_date'] < data['order_date']: raise ValueError('Betaling voor order')
    if reference_mode:
        camt_words(data['bank_reference'])
        return dict(data)
    from operations.tax_reference import TAX_IBANS
    bank = iban(data['iban'])
    if bank in TAX_IBANS:
        raise ValueError('Belastingdienst-IBAN mag niet naar BACS')
    return {**data, 'iban': bank}


def authenticate(headers, raw, now=None):
    key = os.getenv('WOO_IBAN_SHARED_SECRET', '')
    if len(key) < 32: raise HTTPException(503, 'Exact-vervolgactie is nog niet verbonden')
    stamp = headers.get('x-mbom-timestamp','')
    if not re.fullmatch(r'\d{10}', stamp) or abs((now or time.time())-int(stamp)) > 300: raise HTTPException(401, 'Ongeldige tijdstempel')
    expected = hmac.new(key.encode(), stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, headers.get('x-mbom-signature','')): raise HTTPException(401, 'Ongeldige ondertekening')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_woo_iban_events (
        event_id TEXT PRIMARY KEY, order_id BIGINT NOT NULL UNIQUE,
        body JSONB NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', reason TEXT,
        rule_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        next_check TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


def can_upgrade_iban(old, new, state):
    """A confirmed IBAN job may gain a bosci rule for the same proven payment."""
    if state != 'done' or not isinstance(old, dict) or 'iban' not in old or 'bank_reference' in old:
        return False
    if 'bank_reference' not in new or new.get('origin') != 'plugin_completed':
        return False
    fields = ('transaction_id','administration_id','financial_account_id','order_id',
              'order_number','order_date','payment_date','amount','currency','payment_method')
    return all(old.get(k) == new.get(k) for k in fields)


@router.post(PATH)
@router.post(REFERENCE_PATH)
async def receive(request: Request):
    if int(request.headers.get('content-length','0') or 0) > 8192: raise HTTPException(413,'Bericht te groot')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192: raise HTTPException(413,'Bericht te groot')
    raw = bytes(raw)
    authenticate(request.headers, raw)
    try: body = validate(json.loads(raw), reference_mode=request.url.path == REFERENCE_PATH)
    except ValueError as exc: raise HTTPException(422,str(exc)) from None
    except (TypeError, KeyError): raise HTTPException(422,'Ongeldig betalingsbewijs of herkenningscode') from None
    from app import main as app_module
    if not app_module.DATABASE_URL: raise HTTPException(503,'Duurzame wachtrij niet beschikbaar')
    event_id = ADMINISTRATION + ':' + body['transaction_id']
    # Polls must carry exactly the original evidence, not newly computed order state.
    digest = hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    try:
        with app_module._db_connect() as conn:
            initialize(conn)
            conn.execute('''INSERT INTO jnp_woo_iban_events(event_id,order_id,body,digest)
                VALUES(%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING''',
                (event_id,body['order_id'],json.dumps(body),digest))
            row = conn.execute('SELECT digest,state,reason,rule_id,body FROM jnp_woo_iban_events WHERE event_id=%s',(event_id,)).fetchone()
            if row and row[0] != digest and can_upgrade_iban(row[4], body, row[1]):
                # Atomic conversion of a finished job only. An uncertain IBAN write
                # must be reconciled before a different rule can be requested.
                conn.execute("""UPDATE jnp_woo_iban_events SET body=%s::jsonb,digest=%s,
                    state='pending',reason='Bosci-regel voor bevestigde betaling aangevraagd',
                    rule_id=NULL,attempts=0,next_check=NOW(),updated_at=NOW()
                    WHERE event_id=%s AND digest=%s AND state='done'""",
                    (json.dumps(body),digest,event_id,row[0]))
                row = conn.execute('SELECT digest,state,reason,rule_id,body FROM jnp_woo_iban_events WHERE event_id=%s',(event_id,)).fetchone()
            if row is None or row[0] != digest: raise HTTPException(409,'Transactie of order heeft al een ander betalingsbewijs')
            return {'event_id':event_id,'state':row[1],'message':row[2], 'rule_id':row[3], 'debtor':DEBTOR}
    except HTTPException: raise
    except Exception: raise HTTPException(503,'Wachtrij tijdelijk niet beschikbaar') from None


class ExactAPI:
    def __init__(self, app, *, role='woo-rules', priority='routine', floor=200):
        self.app=app; self.last_request=0
        self.role, self.priority, self.floor = role, priority, floor

    async def request(self, method, url, params=None, payload=None):
        # Never automatically repeat a POST. Ambiguous outcomes require reconciliation.
        await asyncio.sleep(max(0,1.2-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        token = await self.app._access_token()
        async with httpx.AsyncClient(timeout=40,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
            from operations.worker_coordination import budgeted_http
            r=await budgeted_http(self.app, self.role, method,
                lambda: client.request(method,url,params=params,json=payload,
                    headers={'Authorization':'Bearer '+token,'Accept':'application/json'}),
                priority=self.priority, floor=self.floor)
        if r.status_code not in (200,201,204): raise RuntimeError('Exact HTTP '+str(r.status_code))
        return r.json() if r.content else {}

    async def rows(self, resource, beta=False, params=None):
        root=f'{BASE}/api/v1/' + ('beta/' if beta else '') + f'{DIVISION}/{resource}'
        url=root; seen=set(); result=[]
        for _ in range(100):
            if url in seen or urlparse(url).scheme!='https' or urlparse(url).netloc!='start.exactonline.nl' or urlparse(url).path!=urlparse(root).path: raise RuntimeError('Invalid pagination')
            seen.add(url)
            p=await self.request('GET',url,params=params)
            d=p.get('d'); batch=d.get('results') if isinstance(d,dict) else d
            if not isinstance(batch,list): raise RuntimeError('Invalid Exact collection')
            result.extend(batch)
            url=(d.get('__next') if isinstance(d,dict) else None) or p.get('__next')
            if not url: return result
            params=None
        raise RuntimeError('Incomplete Exact pagination')

    async def account(self):
        rows=await self.rows('crm/Accounts',params={'$filter':"Code eq '"+DEBTOR.rjust(18)+"'",'$select':'ID,Code,IsSales,Status'})
        if len(rows)!=1 or str(rows[0].get('Code','')).strip()!=DEBTOR or rows[0].get('IsSales') is not True or rows[0].get('Status')!='C': raise RuntimeError('Destination debtor unavailable')
        return str(UUID(rows[0]['ID']))

    async def rules(self):
        return await self.rows('cashflow/AllocationRule',beta=True,params={'$select':'ID,Account,AccountBankAccount,Words,GLAccount,Costcenter,Costunit,VATCode'})

    async def create(self, account, bank):
        return await self.request('POST',f'{BASE}/api/v1/beta/{DIVISION}/cashflow/AllocationRule',payload={'Account':account,'AccountBankAccount':bank})

    async def create_words(self, account, words):
        if not re.fullmatch(r'bosci_[a-f0-9]{29}', words): raise ValueError('Ongeldige CAMT-code')
        return await self.request('POST',f'{BASE}/api/v1/beta/{DIVISION}/cashflow/AllocationRule',payload={'Account':account,'Words':words})


def existing_rule(rules, bank, account):
    found=[]
    for rule in rules:
        candidate=re.sub(r'\s+','',str(rule.get('AccountBankAccount') or '')).upper()
        if candidate != bank: continue
        if str(rule.get('Account') or '').lower()!=account.lower() or any(rule.get(k) for k in ('Words','GLAccount','Costcenter','Costunit','VATCode')):
            return 'conflict',None
        found.append(rule)
    if found: return 'done',str(found[0]['ID'])
    return 'missing',None


def existing_words_rule(rules, words, account):
    found=[]
    for rule in rules:
        candidate=str(rule.get('Words') or '').strip().lower()
        if not re.search(r'(?<![a-z0-9_])'+re.escape(words)+r'(?![a-z0-9_])',candidate): continue
        if candidate!=words or str(rule.get('Account') or '').lower()!=account.lower() or any(rule.get(k) for k in ('AccountBankAccount','GLAccount','Costcenter','Costunit','VATCode')):
            return 'conflict',None
        found.append(rule)
    if found: return 'done',str(found[0]['ID'])
    return 'missing',None


async def process(conn, api, event_id, body, previous):
    from operations.tax_reference import TAX_IBANS
    if body.get('iban') in TAX_IBANS:
        conn.execute("UPDATE jnp_woo_iban_events SET state='conflict',reason='Belastingdienst-IBAN uitgesloten van BACS',updated_at=NOW() WHERE event_id=%s", (event_id,))
        return
    account=await api.account()
    words=camt_words(body['bank_reference']) if 'bank_reference' in body else None
    def lookup(rules):
        return existing_words_rule(rules,words,account) if words else existing_rule(rules,body['iban'],account)
    state,rule=lookup(await api.rules())
    if state=='conflict':
        conn.execute("UPDATE jnp_woo_iban_events SET state='conflict',reason='Bestaande toewijzingsregel wijkt af; controle nodig',updated_at=NOW() WHERE event_id=%s",(event_id,)); return
    if state=='done':
        conn.execute("UPDATE jnp_woo_iban_events SET state='done',rule_id=%s,reason='Toewijzingsregel voor debiteur 109372 bevestigd',updated_at=NOW() WHERE event_id=%s",(rule,event_id)); return
    if previous in ('creating','uncertain'):
        conn.execute("UPDATE jnp_woo_iban_events SET state='uncertain',reason='Eerdere aanmaak niet bevestigd; handmatige controle nodig',next_check=NOW()+INTERVAL '15 minutes',updated_at=NOW() WHERE event_id=%s",(event_id,)); return
    # Autocommit makes intent durable before network I/O.
    conn.execute("UPDATE jnp_woo_iban_events SET state='creating',reason='Aanmaak wordt uitgevoerd',updated_at=NOW() WHERE event_id=%s",(event_id,))
    try:
        if words: await api.create_words(account,words)
        else: await api.create(account,body['iban'])
        # Verify through collection rather than trusting a response body alone.
        state,rule=lookup(await api.rules())
        if state!='done': raise RuntimeError('Readback did not confirm creation')
        conn.execute("UPDATE jnp_woo_iban_events SET state='done',rule_id=%s,reason='Toewijzingsregel voor debiteur 109372 bevestigd',updated_at=NOW() WHERE event_id=%s",(rule,event_id))
    except Exception:
        conn.execute("UPDATE jnp_woo_iban_events SET state='uncertain',reason='Exact-aanmaak niet bevestigd; we controleren opnieuw zonder dubbel aanmaken',next_check=NOW()+INTERVAL '5 minutes',updated_at=NOW() WHERE event_id=%s",(event_id,))


async def cycle(app):
    if os.getenv('ENABLE_WOO_IBAN_RULE_WRITES','false').lower()!='true': STATUS['state']='disabled'; return
    if app.DIVISION!=DIVISION or app.BASE_URL!=BASE or not app.DATABASE_URL: STATUS['state']='configuration_error'; return
    with app._db_connect() as conn:
        initialize(conn)
        if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]: return
        try:
            rows=conn.execute("SELECT event_id,body,state FROM jnp_woo_iban_events WHERE state IN ('pending','creating','uncertain') AND next_check<=NOW() ORDER BY created_at LIMIT 5").fetchall()
            api=ExactAPI(app)
            for event_id,body,state in rows:
                conn.execute("UPDATE jnp_woo_iban_events SET attempts=attempts+1,next_check=NOW()+INTERVAL '5 minutes' WHERE event_id=%s",(event_id,))
                try: await process(conn,api,event_id,body,state)
                except Exception:
                    conn.execute("UPDATE jnp_woo_iban_events SET reason='Exact tijdelijk niet beschikbaar; nieuwe poging gepland',updated_at=NOW() WHERE event_id=%s",(event_id,))
            STATUS['state']='ready'
        finally: conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))


async def serve(app):
    while True:
        try: await cycle(app)
        except asyncio.CancelledError: raise
        except Exception: STATUS['state']='queue_error'
        await asyncio.sleep(15)


@router.post('/api/woocommerce/iban-rule/check')
@router.post('/api/woocommerce/allocation-rule/status')
async def check_connection(request: Request):
    # Authenticated, read-only status; never return credentials or full bank data.
    authenticate(request.headers,b'{}')
    from app import main as app_module
    if app_module.DIVISION!=DIVISION or not app_module.DATABASE_URL: raise HTTPException(503,'Verkeerde administratie of ontbrekende database')
    try:
        api=ExactAPI(app_module)
        account=await api.account()
        rules=await api.rules()
        result={'ok':True,'division':DIVISION,'debtor':DEBTOR,'allocation_rules_readable':True,'rule_count':len(rules),'writes_enabled':os.getenv('ENABLE_WOO_IBAN_RULE_WRITES','false').lower()=='true','allocation_rule_version':3,'rule_types':['iban','bosci_words']}
        if request.url.path.endswith('/allocation-rule/status'):
            result['bosci_rule_count']=sum(1 for r in rules if str(r.get('Account') or '').lower()==account.lower() and re.fullmatch(r'bosci_[a-f0-9]{29}',str(r.get('Words') or '').strip().lower()))
            with app_module._db_connect() as conn:
                result['queue_counts']={state:count for state,count in conn.execute('SELECT state,count(*) FROM jnp_woo_iban_events GROUP BY state').fetchall()}
                rows=conn.execute("SELECT event_id,body->>'order_number',body->>'bank_reference',state,reason,rule_id FROM jnp_woo_iban_events ORDER BY created_at DESC LIMIT 20").fetchall()
                result['recent']=[dict(zip(('event_id','order_number','bank_reference','state','reason','rule_id'),row)) for row in rows]
        return result
    except Exception: raise HTTPException(503,'Exact-verbinding of toewijzingsregels niet beschikbaar') from None
