"""Daily two-sided receipt coverage audit, including already verified batches.

Read-only Exact/Metorik access. The original import is immutable. A fresh review
provides a content-addressed order snapshot for a separate, fenced follow-up.
"""
import asyncio
import base64
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from operations import agent_jobs as jobs, nightly_batches as n, worker_write_fence as fence
from operations.fibonatix_daily_source import prepare, read_source, require
from operations import fibonatix_daily as daily


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_fibonatix_daily_reviews(
        division integer NOT NULL,processing_date date NOT NULL,review_date date NOT NULL,
        state text NOT NULL,data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(division,processing_date,review_date))''')


def classify(source, orders, eligible, ledger, sales, opened, day):
    byid={str(o['order_id']):o for o in orders}
    valid={r['payment_id'] for r in eligible}
    rows=[];refs=set()
    for src in source:
        if src['Type'] not in {'SL','RF'} or src['Status(approved/declined)']!='Approved' or src['Status Code']!='20000':
            continue
        o=byid.get(src['Brand TRX ID'])
        if not o:
            rows.append({'payment_id':src['TRX ID'],'state':'own_order_missing'});continue
        ref='TD'+str(o['order_number']).lstrip('#')
        if src['Type']=='SL':refs.add(ref)
        require(re.fullmatch('TD[0-9]+',ref) is not None,'invalid_order_reference')
        amount=Decimal(src['Amount'])
        require(amount.is_finite() and amount>0 and amount==amount.quantize(Decimal('.01')),'invalid_source_amount')
        row={'payment_id':src['TRX ID'],'woo_id':o['order_id'],'ref':ref,'date':day.isoformat(),
             'amount':str((amount if src['Type']=='SL' else -amount).quantize(Decimal('.01'))),'order_status':o['status'],'source_kind':src['Type']}
        check=daily.reconcile([row],ledger)
        if check['errors'] or check['other_receipts_same_order']:state='receipt_conflict'
        elif check['existing']:state='imported'
        elif row['payment_id'] in valid:state='ready_for_import'
        else:state='source_order_review'
        row.update(state=state,open_sales=[s['EntryNumber'] for s in sales if s['YourRef']==ref and any(x['EntryNumber']==s['EntryNumber'] and Decimal(str(x['Amount']))>0 for x in opened)])
        rows.append(row)
    own_orders={'TD'+str(o['order_number']).lstrip('#'):o for o in orders}
    unexplained=[]
    for sale in sales:
        ref=sale.get('YourRef');o=own_orders.get(ref)
        if sale.get('Type')!=20 or sale.get('Reversal') is not False:continue
        if o and o['payment_method']!='wc_fibonatix':continue
        if ref not in refs:
            unexplained.append({'entry':sale['EntryNumber'],'ref':ref,'state':'source_payment_missing' if o else 'own_order_missing'})
    return {'receipts':rows,'sales_without_day_payment':unexplained,
            'complete':all(r['state']=='imported' for r in rows) and not unexplained,
            'ready_ids':[r['payment_id'] for r in rows if r['state']=='ready_for_import']}


async def run(app,job):
    after=job['params'].get('after_revision')
    require(job['action']=='daily_review' and set(job['params'])==({'date','review_date','after_revision'} if after else {'date','review_date'}),'invalid_review_job')
    if after:require(re.fullmatch('[0-9a-f]{64}',after) is not None,'invalid_after_revision')
    day=date.fromisoformat(job['params']['date']);review_day=date.fromisoformat(job['params']['review_date'])
    require(job['task_key']==n.identity(day,'fibonatix') and day<review_day<=datetime.now(n.ZONE).date(),'invalid_review_day')
    require(fence.current_owner() is not None and fence.current_owner().role=='fibonatix','assigned_worker_required')
    with app._db_connect() as conn:
        initialize(conn)
        prior=conn.execute('SELECT state,data FROM jnp_fibonatix_daily_reviews WHERE division=%s AND processing_date=%s AND review_date=%s',(n.DIVISION,day,review_day)).fetchone()
        if prior and not after:return {'state':prior[0],'already_reviewed':True,'financial_writes':False}
        if after:
            require(prior and prior[1].get('revision')==after,'prior_review_changed')
            followup=conn.execute('SELECT state FROM jnp_fibonatix_daily_followups WHERE division=%s AND processing_date=%s AND revision=%s',(n.DIVISION,day,after)).fetchone()
            require(followup and followup[0]=='verified','followup_not_verified')
        key=n.identity(day,'fibonatix')+':source'
        src=conn.execute('SELECT result FROM paragon_login_probes WHERE probe_id=%s',(key,)).fetchone()[0]
        old=conn.execute('SELECT result FROM paragon_login_probes WHERE probe_id=%s',(key+':orders',)).fetchone()[0]
        original=conn.execute('SELECT state FROM jnp_fibonatix_daily_imports WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchone()
        require(original and original[0]=='verified','original_import_not_verified')
        refund_refs={}
        past=conn.execute("SELECT data FROM fibonatix_import_artifacts WHERE name='apply'").fetchall()
        for (artifact,) in past:
            if artifact.get('summary',{}).get('state')!='import_verified':continue
            for row in artifact.get('artifacts',{}).get('manifest',[]):
                if row.get('woo_id') and row.get('ref'):refund_refs[str(row['woo_id'])]=row['ref']
        for (data,) in conn.execute("SELECT data FROM jnp_fibonatix_daily_imports WHERE division=%s AND state='verified'",(n.DIVISION,)).fetchall():
            for row in data.get('candidates',[]):
                refund_refs[str(row['woo_id'])]=row['ref']
    require(src.get('source_window_verified') and src.get('source_timestamp_timezone')=='UTC','source_not_verified')
    raw=base64.b64decode(src['source_csv'],validate=True)
    require(hashlib.sha256(raw).hexdigest()==src['source_sha256'],'source_changed')
    selected,source_summary=read_source(raw,day,utc_ui_proof=src['utc_ui_proof'])
    api=daily.API(app)
    sales=await api.rows('salesentry/SalesEntries',{'$filter':"Customer eq guid'"+daily.DEBTOR+"' and EntryDate ge datetime'"+day.isoformat()+"T00:00:00' and EntryDate lt datetime'"+(day+timedelta(days=1)).isoformat()+"T00:00:00'",'$select':'EntryID,EntryNumber,YourRef,AmountFC,Currency,Type,Reversal'})
    opened=await api.rows('read/financial/ReceivablesList',{'$filter':"AccountId eq guid'"+daily.DEBTOR+"'",'$select':'AccountId,EntryNumber,YourRef,Amount,JournalCode'})
    refs={s['YourRef'] for s in sales if re.fullmatch('TD[0-9]{4,10}',str(s.get('YourRef') or ''))}
    refs.update('TD'+str(o['order_number']).lstrip('#') for o in old['orders'])
    refs.update(refund_refs[s['Brand TRX ID']] for s in selected if s['Type']=='RF' and s['Brand TRX ID'] in refund_refs)
    from operations.metorik_bacs_evidence import lookup_orders
    orders={};refs=sorted(refs)
    for i in range(0,len(refs),100):orders.update((await lookup_orders(refs[i:i+100]))['orders'])
    orders=list(orders.values())
    eligible,exceptions,summary=prepare(raw,orders,day,utc_ui_proof=src['utc_ui_proof'])
    good=[s for s in selected if s['Type'] in {'SL','RF'} and s['Status(approved/declined)']=='Approved' and s['Status Code']=='20000']
    ledger=[]
    for i in range(0,len(good),12):
        query=' or '.join("PaymentReference eq '"+s['TRX ID']+"'" for s in good[i:i+12])
        ledger+=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':daily.SELECT})
    coverage=classify(selected,orders,eligible,ledger,sales,opened,day)
    captured=datetime.now(timezone.utc).isoformat()
    snapshot={'state':'read_complete','captured_at':captured,'orders':orders,'missing_ids':[], 'source_sha256':src['source_sha256']}
    revision=hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    data={**coverage,'source_summary':source_summary,'summary':summary,'exceptions':exceptions,
          'revision':revision,'captured_at':captured,'source_sha256':src['source_sha256'],
          'source_captured_at':src.get('captured_at'),'source_refreshed':False,
          'sales':sales,'opened':opened,'ledger':ledger,'financial_writes':False}
    # Missing source receipts remain explicit, even if every uploaded line verified.
    state='complete' if coverage['complete'] else 'incomplete'
    with app._db_connect() as conn,conn.transaction():
        previous=conn.execute('SELECT data FROM jnp_fibonatix_daily_imports WHERE division=%s AND processing_date=%s FOR UPDATE',(n.DIVISION,day)).fetchone()[0].get('dashboard_exceptions',[])
        by_payment={r.get('payment_transaction_id'):r for r in previous}
        covered={r['payment_id'] for r in coverage['receipts']}
        display=[r for r in previous if r.get('payment_transaction_id') not in covered]
        for receipt in coverage['receipts']:
            if receipt['state']=='imported' and receipt.get('source_kind')!='RF':continue
            item=dict(by_payment.get(receipt['payment_id'],{}))
            status=('refund_imported_review' if receipt['state']=='imported' and receipt.get('source_kind')=='RF' else receipt['state'])
            labels={'refund_imported_review':'Refund ingelezen; eigen creditnota/afwikkeling controleren','waiting_for_completed_order':'Betaald; order nog niet afgerond',
                    'refund_or_cancel_review':'Refund of annulering beoordelen',
                    'ready_for_import':'Betaling ontbreekt; gereed voor aanvullende import'}
            actions={'refund_imported_review':'Controleer oorspronkelijke ontvangst en eigen creditnota. Refund staat apart op bestaande rekening 1350; geen aflettering of saldering tussen orders uitgevoerd.','waiting_for_completed_order':'Controleer verzending/afronding van de eigen order; dagelijkse hercontrole blijft actief.',
                     'refund_or_cancel_review':'Controleer eigen ontvangst, PSP-refund en creditnota volgens refundbeleid.',
                     'ready_for_import':'Gebruik de verse review-revisie voor een afzonderlijke aanvullende import; herhaal de originele import niet.'}
            item.update(case_key=receipt['payment_id'],payment_transaction_id=receipt['payment_id'],
                        reference=receipt.get('ref'),amount=receipt.get('amount'),woo_order_id=receipt.get('woo_id'),
                        order_status=receipt.get('order_status'),receipt_imported=receipt['state']=='imported',
                        division=n.DIVISION,journal='Fibonatix',journal_code='26',account_code=None if receipt.get('source_kind')=='RF' else '100100',gl_account='1350' if receipt.get('source_kind')=='RF' else '1100',
                        currency='EUR',bank_date=day.isoformat(),reason=status,
                        status='order_status_review' if status=='waiting_for_completed_order' else 'source_review',
                        status_label=labels.get(status,'Betaling nader beoordelen'),
                        work_group='waiting' if status=='waiting_for_completed_order' else 'review',
                        next_action=actions.get(status,'Controleer bronbetaling en eigen verkoopboeking; niet blind herhalen.'),
                        observed_at=captured,review_revision=revision)
            display.append(item)
        data['previous_dashboard_exceptions']=previous
        data['dashboard_exceptions']=display
        conn.execute("UPDATE jnp_fibonatix_daily_imports SET data=jsonb_set(data,'{dashboard_exceptions}',%s::jsonb) WHERE division=%s AND processing_date=%s",(json.dumps(display),n.DIVISION,day))
        conn.execute('INSERT INTO paragon_login_probes(probe_id,result) VALUES(%s,%s::jsonb)',(n.identity(day,'fibonatix')+':review:'+revision,json.dumps(snapshot)))
        if after:
            conn.execute('INSERT INTO paragon_login_probes(probe_id,result) VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING',(n.identity(day,'fibonatix')+':audit:'+after,json.dumps(prior[1])))
            saved=conn.execute("UPDATE jnp_fibonatix_daily_reviews SET state=%s,data=%s::jsonb,updated_at=now() WHERE division=%s AND processing_date=%s AND review_date=%s AND data->>'revision'=%s RETURNING division",(state,json.dumps(data),n.DIVISION,day,review_day,after)).fetchone()
            require(saved,'review_changed_during_readback')
        else:
            conn.execute('INSERT INTO jnp_fibonatix_daily_reviews(division,processing_date,review_date,state,data) VALUES(%s,%s,%s,%s,%s::jsonb)',(n.DIVISION,day,review_day,state,json.dumps(data)))
    return {'state':state,'ready_for_import':len(coverage['ready_ids']),'financial_writes':False}


def seed(app):
    """One audit per original day per local day, after the 01:00 boundary.

    No source download or import is scheduled here. Original imports and all
    continuation writes retain their own explicit durable commands.
    """
    now=datetime.now(n.ZONE)
    if now.hour<1:return
    with app._db_connect() as conn:
        initialize(conn)
        daily.initialize(conn)
        if conn.execute("SELECT 1 FROM jnp_agent_jobs WHERE role='fibonatix' AND state IN ('queued','running','uncertain') LIMIT 1").fetchone():return
        days=conn.execute("SELECT processing_date FROM jnp_fibonatix_daily_imports WHERE division=%s AND state='verified' ORDER BY processing_date",(n.DIVISION,)).fetchall()
        for (day,) in days:
            if day>=now.date():continue
            latest=conn.execute('SELECT state FROM jnp_fibonatix_daily_reviews WHERE division=%s AND processing_date=%s ORDER BY review_date DESC LIMIT 1',(n.DIVISION,day)).fetchone()
            if latest and latest[0]=='complete':continue
            request=n.identity(day,'fibonatix')+':review:'+now.date().isoformat()
            if conn.execute('SELECT 1 FROM jnp_agent_jobs WHERE division=%s AND role=%s AND request_key=%s',(n.DIVISION,'fibonatix',request)).fetchone():continue
            jobs.submit(conn,n.DIVISION,'fibonatix',n.identity(day,'fibonatix'),'daily_review',{'date':day.isoformat(),'review_date':now.date().isoformat()},request)
            return
