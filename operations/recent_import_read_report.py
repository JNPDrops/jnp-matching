"""Single bounded read-only check of imports in the operator's last three hours.

No financial writes, queue updates, routing changes, or public endpoint. The
existing audit table stores only a once-only marker; results use private logs.
"""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from urllib.parse import urlparse
import zlib

from operations import allocation_connection as allocation
from operations import bacs_debtor_transfer as m
from operations import debtor_routing_policy as policy
from operations import metorik_bacs_evidence as evidence

REPORT = 'recent-imports-20261004-172251-202251-v1'
EXPIRES = datetime(2026, 10, 4, 21, tzinfo=timezone.utc)
START_LOCAL = '2026-10-04T17:22:51'
END_LOCAL = '2026-10-04T20:22:51'
# Include the UTC interpretation as boundary evidence; classify the requested
# period using Exact's observed Amsterdam wall-clock Created values.
QUERY_START = '2026-10-04T15:22:51'
FIELDS = ('EntryID','EntryNumber','YourRef','Customer','Created','Modified',
          'PaymentCondition','Currency','AmountFC','Status','Type','Reversal')
LOG = logging.getLogger('uvicorn.error')


def event(name, **fields):
    LOG.info('JNP_RECENT_IMPORTS %s', json.dumps(
        {'report':REPORT,'event':name,**fields}, default=str))


def emit(stage, data):
    raw = json.dumps(data,ensure_ascii=False,separators=(',',':'),default=str).encode()
    packed = base64.b64encode(zlib.compress(raw,9)).decode()
    parts = [packed[i:i+5500] for i in range(0,len(packed),5500)]
    for index, part in enumerate(parts):
        event('data',stage=stage,part=index,parts=len(parts),
              sha256=hashlib.sha256(raw).hexdigest(),data=part)


def wall_clock(value):
    match = re.fullmatch(r'/Date\((-?\d+)\)/', value or '')
    m.require(match is not None, 'Invalid Exact Created timestamp')
    return datetime.fromtimestamp(int(match[1])/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def order_ref(row):
    ref = row.get('YourRef')
    if not isinstance(ref,str) or not re.fullmatch(r'TD\d{4,10}',ref):
        return None
    if row.get('Reversal') is not False:
        return None
    if row.get('Type') == 20:
        return ref
    if row.get('Type') == 21:
        match = re.fullmatch(r'Order #(\d{4,10}) / Credit #(TD\d{4,10})',row.get('Description') or '')
        if match and match[2] == ref:
            return 'TD'+match[1]
    return None


class ReadAPI(m.Exact):
    def __init__(self, app):
        super().__init__(allocation.RoutingApp(app), role='reports', priority='bulk', floor=500)
        self.calls = 0

    async def request(self, method, url, params=None, payload=None):
        p = urlparse(url)
        prefix = f'/api/v1/{m.DIVISION}/'
        m.require(method=='GET' and payload is None and p.scheme=='https'
            and p.netloc=='start.exactonline.nl' and not p.username and not p.fragment
            and p.path in {prefix+'salesentry/SalesEntries',prefix+'crm/Accounts'},
            'Read-only recent-import request rejected')
        m.require(self.calls<40 and self.limits.get('remaining',1000)>500,
                  'Recent-import read budget reached')
        self.calls += 1
        await asyncio.sleep(0.4)
        return await super().request(method,url,params,payload)


async def collect(app):
    api = ReadAPI(app)
    rows = await api.rows('salesentry/SalesEntries',{
        '$filter':f"Created ge datetime'{QUERY_START}' and Created le datetime'{END_LOCAL}'",
        '$select':','.join(FIELDS)+',Description'})
    m.require(len(rows)<1000 and len({r['EntryID'] for r in rows})==len(rows),
              'Recent-import completeness or duplication limit')
    headers = [{**{k:r.get(k) for k in FIELDS},'order_ref':order_ref(r),
                'created_wall_clock':wall_clock(r.get('Created')),
                'in_requested_period':START_LOCAL<=wall_clock(r.get('Created'))<=END_LOCAL}
               for r in rows]
    emit('headers',{'read_at':m.utcnow(),'rows':headers,
        'start_local':START_LOCAL,'end_local':END_LOCAL,'timezone':'Europe/Amsterdam'})
    ids = sorted({m.guid(r['Customer']) for r in rows})
    accounts = []
    for start in range(0,len(ids),20):
        accounts += await api.rows('crm/Accounts',{'$select':'ID,Code',
            '$filter':' or '.join("ID eq guid'"+i+"'" for i in ids[start:start+20])})
    emit('accounts',{'rows':accounts,'read_at':m.utcnow()})
    refs = sorted({h['order_ref'] for h in headers if h['order_ref']})
    orders = {}
    for start in range(0,len(refs),100):
        proof = await evidence.lookup_orders(refs[start:start+100])
        orders.update({n:{k:o.get(k) for k in ('order_id','order_number','payment_method','status')}
                       for n,o in proof['orders'].items()})
    emit('orders',{'rows':orders,'read_at':m.utcnow(),
        'continuous_routes':policy.CONTINUOUS_ROUTES,'retained':policy.RETAIN_ON_SOURCE,
        'cleanup_routes':policy.CLEANUP_ROUTES})
    with app._db_connect() as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            control = conn.execute('SELECT enabled,cursor_at,last_scan,routing_policy FROM jnp_debtor_route_control').fetchone()
            queue = conn.execute('''SELECT entry_id::text,reference,modified,state,
                reason,next_check,work_scope,order_reference,
                order_evidence->>'payment_method' FROM jnp_debtor_route_queue
                WHERE entry_id = ANY(%s::uuid[])''',([r['EntryID'] for r in rows],)).fetchall()
    emit('routing',{'read_at':m.utcnow(),
        'control':dict(zip(('enabled','cursor_at','last_scan','policy'),control)),
        'rows':[dict(zip(('entry_id','reference','modified','state','reason','next_check',
                         'scope','order_reference','stored_method'),q)) for q in queue]})
    event('complete',read_at=m.utcnow(),exact_calls=api.calls,headers=len(headers),
        in_period=sum(h['in_requested_period'] for h in headers),orders=len(orders),
        financial_writes=0,limits=api.limits)


async def run(app):
    if datetime.now(timezone.utc)>=EXPIRES:
        return
    try:
        with app._db_connect() as conn:
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(%s)',(3977752100418,))
                if conn.execute('SELECT 1 FROM jnp_debtor_route_audit WHERE event=%s LIMIT 1',(REPORT,)).fetchone():
                    return
                conn.execute("INSERT INTO jnp_debtor_route_audit(run_id,entry_id,event,body) VALUES(%s,'00000000-0000-0000-0000-000000000000',%s,%s::jsonb)",
                    ('5212110b-39b3-4824-bd9a-3c53d3c266d9',REPORT,'{"read_only_report":true}'))
        event('started')
        await asyncio.wait_for(collect(app),timeout=300)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        event('failed',error_type=type(exc).__name__,
            http_status=exc.status_code if isinstance(exc,m.ExactRequestError) else None,
            reason=str(exc) if isinstance(exc,m.Stop) else 'Read failed; details suppressed')
