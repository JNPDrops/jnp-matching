"""Operator-requested Fibonetics reconciliation snapshot, 4 October 2026.

Exact and Metorik GET only. No routing/policy change, financial writes, public
endpoint, or customer/card details. Bounded one-off output uses private Render
logs; a durable audit marker prevents repeated reads after future deployments.
"""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlparse
import zlib

import httpx
from operations import allocation_connection as allocation
from operations import bacs_debtor_transfer as m

REPORT = 'fibonetics-20261004-1150-v4'
EXPIRES = datetime(2026,10,4,13,tzinfo=timezone.utc)
LOCK = 3977752100401
LOG = logging.getLogger('uvicorn.error')
ORDER_FIELDS = ('order_id','order_number','payment_method','status','currency',
                'total','total_refunds','order_created_at','order_updated_at',
                'order_paid_at','order_completed_at','created_via')
HEADER_FIELDS = ('EntryID','EntryNumber','YourRef','Customer','EntryDate',
                 'PaymentCondition','Currency','AmountFC','Status','Type','Reversal')
OPEN_FIELDS = ('AccountId','AccountCode','YourRef','EntryNumber','InvoiceDate',
               'Amount','CurrencyCode','JournalCode')
READS = {'crm/Accounts','salesentry/SalesEntries','read/financial/ReceivablesList'}


def event(name, **fields):
    LOG.info('JNP_FIBO_REPORT %s',json.dumps({'report':REPORT,'event':name,**fields},default=str))


def emit(stage, data):
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':'),default=str).encode()
    packed=base64.b64encode(zlib.compress(raw,9)).decode()
    chunks=[packed[i:i+5500] for i in range(0,len(packed),5500)]
    for i,chunk in enumerate(chunks):
        event('data',stage=stage,part=i,parts=len(chunks),sha256=hashlib.sha256(raw).hexdigest(),data=chunk)


class ReadAPI(m.Exact):
    def __init__(self,app):
        super().__init__(allocation.RoutingApp(app), role='reports', priority='bulk', floor=500)
        self.calls=0

    async def request(self,method,url,params=None,payload=None):
        p=urlparse(url)
        prefix=f'/api/v1/{m.DIVISION}/'
        m.require(method=='GET' and payload is None and p.scheme=='https'
                  and p.netloc=='start.exactonline.nl' and not p.username and not p.fragment
                  and p.path.startswith(prefix) and p.path[len(prefix):] in READS,
                  'Read-only report request rejected')
        m.require(self.calls<250 and self.limits.get('remaining',1000)>500,
                  'Read-only report API budget reached')
        await asyncio.sleep(0.4)
        self.calls+=1
        return await super().request(method,url,params,payload)


class OrderReader:
    def __init__(self):
        self.calls=0
        self.orders={}

    async def get(self,client,path,params=None):
        m.require(path in ('','/orders') and self.calls<200,'Order read limit')
        await asyncio.sleep(1.5)
        self.calls+=1
        r=await client.get('https://app.metorik.com/api/v1/store'+path,params=params)
        m.require(r.status_code==200,'Metorik read failed; response suppressed')
        return r.json()

    async def pages(self,client,filters,wanted_ids,wanted_refs,stop_before=None):
        seen=set()
        previous_date=None
        for page in range(1,101):
            body=await self.get(client,'/orders',{'page':page,'per_page':100,
                'order_by':'order_created_at','order_dir':'desc' if stop_before else 'asc','filters':json.dumps(filters)})
            rows,pg=body.get('data'),body.get('pagination')
            m.require(isinstance(rows,list) and isinstance(pg,dict)
                and pg.get('current_page')==page and pg.get('per_page')==100
                and type(pg.get('has_more_pages')) is bool,'Invalid order pagination')
            for raw in rows:
                if stop_before:
                    date=str(raw.get('order_created_at') or '')
                    m.require(bool(date) and (previous_date is None or date<=previous_date), 'Order chronology not respected')
                    previous_date=date
                oid=raw.get('order_id');number=str(raw.get('order_number') or '').lstrip('#')
                m.require(type(oid) is int and oid not in seen,'Ambiguous paginated order identity')
                seen.add(oid)
                if oid in wanted_ids or 'TD'+number in wanted_refs:
                    self.orders[oid]={k:raw.get(k) for k in ORDER_FIELDS}
            if page%10==0 or not pg['has_more_pages']:
                event('order_progress',calls=self.calls,read_in_query=len(seen),retained=len(self.orders),more=pg['has_more_pages'])
            if not pg['has_more_pages'] or (stop_before and (previous_date[:10]<stop_before or wanted_ids<=set(self.orders))):
                return
            m.require(bool(rows),'Empty intermediate order page')
        raise m.Stop('Incomplete Metorik read')


def project_headers(rows):
    result=[]
    for r in rows:
        item={k:r.get(k) for k in HEADER_FIELDS}
        match=re.fullmatch(r'Order #([0-9]{4,10}) / Credit #TD[0-9]{4,10}',str(r.get('Description') or ''))
        item['original_order_ref']='TD'+match[1] if match else None
        result.append(item)
    return result


async def collect(app):
    api=ReadAPI(app)
    started=m.utcnow()
    accounts=await api.rows('crm/Accounts',{'$filter':"Code eq '"+'100100'.rjust(18)+"'",'$select':'ID,Code'})
    m.require(len(accounts)==1 and accounts[0]['Code'].strip()=='100100','Source account ambiguous')
    source=m.guid(accounts[0]['ID'])
    open_rows=await api.rows('read/financial/ReceivablesList',{
        '$filter':f"AccountId eq guid'{source}'",'$select':','.join(OPEN_FIELDS)})
    m.require(len(open_rows)<10000,'Open-item completeness limit reached')
    emit('open_100100',{'read_at':m.utcnow(),'rows':[{k:r.get(k) for k in OPEN_FIELDS} for r in open_rows]})
    headers=await api.rows('salesentry/SalesEntries',{'$filter':f"Customer eq guid'{source}' and EntryDate ge datetime'2026-09-15T00:00:00'",
        '$select':','.join(HEADER_FIELDS)+',Description'})
    m.require(len(headers)<10000 and len({r['EntryID'] for r in headers})==len(headers),
              'Sales-entry completeness limit reached')
    # Retain every currently open source item, even when its booking is older
    # than the PSP export. Paid older bookings are queried separately by CSV order.
    present_entries={r['EntryNumber'] for r in headers}
    older=sorted({r['EntryNumber'] for r in open_rows}-present_entries)
    for start in range(0,len(older),20):
        batch=older[start:start+20]
        headers+=await api.rows('salesentry/SalesEntries',{
            '$filter':f"Customer eq guid'{source}' and ("+' or '.join(f'EntryNumber eq {int(n)}' for n in batch)+')',
            '$select':','.join(HEADER_FIELDS)+',Description'})
    m.require(len({r['EntryID'] for r in headers})==len(headers),'Duplicate source headers')
    headers=project_headers(headers)
    emit('headers_100100',{'read_at':m.utcnow(),'rows':headers,
         'scope':'All source sales entries dated since 2026-09-15 plus all older currently open source sales entries'})
    refs={ref for h in headers for ref in (h.get('YourRef'),h.get('original_order_ref'))
          if isinstance(ref,str) and re.fullmatch(r'TD[0-9]{4,10}',ref)}
    wanted=set(json.loads(Path(__file__).with_name('fibonetics_report_ids.json').read_text()))
    reader=OrderReader()
    key=os.environ.get('METORIK_API_KEY','').strip()
    m.require(bool(key),'Metorik reader unavailable')
    async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False,
        verify=m.TLS_CONTEXT,headers={'Authorization':'Bearer '+key,'Accept':'application/json'}) as client:
        store=await reader.get(client,'')
        m.require({k:store.get(k) for k in ('name','timezone','currency','platform')}==
                  {'name':'TheDrops.eu','timezone':'Europe/Amsterdam','currency':'EUR','platform':'woocommerce'},
                  'Unexpected Metorik store')
        await reader.pages(client,[],wanted,refs,stop_before='2026-09-01')
        found={'TD'+str(o['order_number']).lstrip('#') for o in reader.orders.values()}
        missing=sorted(refs-found)
        for start in range(0,len(missing),25):
            await reader.pages(client,[{'field':'order_number','operator':'in',
                'value':[r[2:] for r in missing[start:start+25]]}],wanted,refs)
    emit('orders',{'read_at':m.utcnow(),'calls':reader.calls,'rows':list(reader.orders.values()),
                   'export_ids_unresolved':sorted(wanted-set(reader.orders))})
    # Search expected order references across every debtor, including paid entries.
    all_refs={'TD'+str(o['order_number']).lstrip('#') for oid,o in reader.orders.items() if oid in wanted}
    present={h['YourRef'] for h in headers}
    missing=sorted(all_refs-present)
    other=[]
    for start in range(0,len(missing),20):
        batch=missing[start:start+20]
        other+=project_headers(await api.rows('salesentry/SalesEntries',{
            '$filter':' or '.join('YourRef eq '+m.quoted(ref) for ref in batch),
            '$select':','.join(HEADER_FIELDS)+',Description'}))
    ids=sorted({h['Customer'] for h in other}-{source})
    other_accounts=[]
    for start in range(0,len(ids),20):
        other_accounts+=await api.rows('crm/Accounts',{'$select':'ID,Code',
            '$filter':' or '.join("ID eq guid'"+m.guid(i)+"'" for i in ids[start:start+20])})
    emit('other_headers',{'read_at':m.utcnow(),'rows':other,'accounts':accounts+other_accounts})
    event('complete',started=started,completed=m.utcnow(),exact_calls=api.calls,
          metorik_calls=reader.calls,open_items=len(open_rows),source_headers=len(headers),
          other_headers=len(other),orders=len(reader.orders),export_ids_unresolved=len(wanted-set(reader.orders)),
          financial_writes=0,limits=api.limits)


async def run(app):
    if datetime.now(timezone.utc)>=EXPIRES:
        return
    # Use the existing audit table only as a one-off execution marker. Do not
    # change queue states, routing switches, balances or source transactions.
    try:
        return await asyncio.wait_for(claim_and_collect(app),timeout=1200)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        event('failed',error_type=type(exc).__name__,
              http_status=exc.status_code if isinstance(exc,m.ExactRequestError) else None,
              reason=str(exc) if isinstance(exc,m.Stop) else 'Read failed; details suppressed')


async def claim_and_collect(app):
    with app._db_connect() as conn:
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
            if conn.execute("SELECT 1 FROM jnp_debtor_route_audit WHERE event=%s LIMIT 1",(REPORT,)).fetchone():
                return
            conn.execute("INSERT INTO jnp_debtor_route_audit(run_id,entry_id,event,body) VALUES(%s,'00000000-0000-0000-0000-000000000000',%s,%s::jsonb)",
                         ('5bb37b58-e49e-4cf3-98e4-c5a688371194',REPORT,'{"read_only_report":true}'))
    event('started')
    await collect(app)
    return True
