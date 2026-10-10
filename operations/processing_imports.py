"""Immutable cutoff imports, separate from historical calendar-day tasks.

Reuses the established journal XML and Exact readback validators. This adapter
never resets a historical task and deduplicates using the PSP transaction ID.
"""
import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from operations import worker_write_fence as fence, routing_completion
from operations.worker_coordination import budgeted_http


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_sources (
        batch_key text NOT NULL,psp text NOT NULL,state text NOT NULL,
        data jsonb NOT NULL DEFAULT '{}'::jsonb,updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(batch_key,psp),CHECK(psp IN ('fibonatix','icepay')))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_imports (
        batch_key text NOT NULL,psp text NOT NULL,part text NOT NULL,
        state text NOT NULL,write_requested boolean NOT NULL DEFAULT false,
        data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(batch_key,psp,part),CHECK(psp IN ('fibonatix','icepay')),
        CHECK(part IN ('receipts','refunds')))''')


def bounded_rows(rows, start, end):
    require(start.tzinfo is not None and end.tzinfo is not None and start<end,'invalid_source_window')
    seen=set(); selected=[]
    for row in rows:
        require(isinstance(row.get('payment_id'),str) and row['payment_id'] not in seen,'duplicate_source_payment_id')
        seen.add(row['payment_id'])
        value=row.get('source_time_utc')
        require(isinstance(value,str),'source_timestamp_required')
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        require(stamp.tzinfo is not None,'source_timestamp_must_be_aware')
        if start<=stamp<end:
            selected.append(row)
    return selected


class Store:
    def __init__(self,conn,key,psp,part):
        self.conn,self.args=conn,(key,psp,part)

    def prior(self):
        return self.conn.execute('''SELECT state,write_requested,data FROM jnp_processing_imports
            WHERE batch_key=%s AND psp=%s AND part=%s''',self.args).fetchone()

    def save(self,state,data):
        self.conn.execute('''UPDATE jnp_processing_imports SET state=%s,data=%s::jsonb,updated_at=now()
            WHERE batch_key=%s AND psp=%s AND part=%s''',(state,json.dumps(data),*self.args))
        self.conn.commit()

    def insert(self,data):
        self.conn.execute('''INSERT INTO jnp_processing_imports(batch_key,psp,part,state,data)
            VALUES(%s,%s,%s,'preparing',%s::jsonb)''',(*self.args,json.dumps(data)))
        self.conn.commit()

    def claim(self,data):
        row=self.conn.execute('''UPDATE jnp_processing_imports
            SET state='upload_requested',write_requested=true,data=%s::jsonb,updated_at=now()
            WHERE batch_key=%s AND psp=%s AND part=%s AND state='prepared'
            AND NOT write_requested RETURNING batch_key''',(json.dumps(data),*self.args)).fetchone()
        self.conn.commit()
        require(row,'prior_import_intent_requires_readback')


async def existing(api,module,rows,day,journal):
    # Do not repeatedly scan the whole growing journal year.
    # Native matching may alter reference fields. Also inspect the bounded
    # booking day so an existing described PSP ID cannot be mistaken for new.
    tomorrow=day+timedelta(days=1)
    ledger=await api.rows('financialtransaction/TransactionLines',{
        '$filter':f"JournalCode eq '{journal}' and Date ge datetime'{day.isoformat()}T00:00:00' and Date lt datetime'{tomorrow.isoformat()}T00:00:00'",
        '$select':module.SELECT})
    for index in range(0,len(rows),12):
        ids=rows[index:index+12]
        query=' or '.join("PaymentReference eq '"+r['payment_id']+"'" for r in ids)
        ledger+=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':module.SELECT})
    return list({line['ID']:line for line in ledger}.values())


async def latest_entry(api,day,journal):
    # $orderby descending establishes the maximum in the first row. Do not
    # follow a server-supplied next link and walk the growing whole journal.
    raw=await api.get('financialtransaction/TransactionLines',{
        '$filter':f"JournalCode eq '{journal}' and FinancialYear eq {day.year}",
        '$select':'EntryNumber','$orderby':'EntryNumber desc','$top':'1'})
    data=raw.get('d',{})
    rows=data.get('results') if isinstance(data,dict) else data
    require(isinstance(rows,list) and len(rows)==1 and type(rows[0].get('EntryNumber')) is int,'entry_sequence_missing')
    entry=rows[0]['EntryNumber']+1
    require(str(entry).startswith(str(day.year)[-2:]+journal),'entry_sequence_unexpected')
    return entry


def check(module,psp,rows,ledger):
    if psp=='fibonatix':
        result=module.reconcile(rows,ledger)
        return result['existing'],result['errors']
    manifest=module.manifest_for_existing(rows,ledger)
    result=module.reconcile(manifest,ledger)
    return result['verified_ids'],result['errors']


async def _run(app,key,psp,part,reconcile_only=False):
    from operations import fibonatix_daily,icepay_daily,task_drain
    module=fibonatix_daily if psp=='fibonatix' else icepay_daily
    journal='26' if psp=='fibonatix' else '27'
    debtor='100100' if psp=='fibonatix' else '109419'
    require(app.DIVISION==3977752 and app.BASE_URL==module.BASE,'wrong_administration')
    owner=fence.current_owner()
    require(owner is not None and owner.role==psp,'assigned_psp_worker_required')
    conn=app._db_connect(); locked=False
    lock_sql=('SELECT pg_try_advisory_lock(397775226)' if psp=='fibonatix' else
              "SELECT pg_try_advisory_lock(hashtextextended('jnp:3977752:icepay:receipts',0))")
    try:
        locked=conn.execute(lock_sql).fetchone()[0]
        require(locked,'another_psp_import_writer')
        initialize(conn)
        saved=conn.execute('''SELECT processing_date,window_start,window_end FROM jnp_processing_batches
            WHERE division=3977752 AND batch_key=%s''',(key,)).fetchone()
        require(saved,'registered_batch_required')
        day,start,end=saved
        source=conn.execute('SELECT state,data FROM jnp_processing_sources WHERE batch_key=%s AND psp=%s',(key,psp)).fetchone()
        require(source and source[0]=='verified','verified_source_required')
        payload=source[1]
        require(payload.get('window_start')==start.isoformat() and payload.get('window_end')==end.isoformat(),'source_window_changed')
        rows=bounded_rows(payload['candidates'],start,end)
        rows=[r for r in rows if (r.get('source_kind')=='RF')==(part=='refunds')]
        require(all(r['date']==day.isoformat() for r in rows),'source_booking_day_changed')
        store=Store(conn,key,psp,part);prior=store.prior()
        if prior and prior[0]=='verified':
            return {'state':'verified','already_completed':True,'financial_writes':False}
        require(not prior or not prior[1] or reconcile_only,'prior_import_intent_requires_readback')
        evidence={'source_digest':digest(payload),'candidates':rows,'imported':0,'existing':0}
        if prior:
            require(prior[2]['source_digest']==evidence['source_digest'] and prior[2]['candidates']==rows,'import_source_changed')
            evidence=prior[2]
        else:
            require(not reconcile_only,'no_import_to_reconcile')
            store.insert(evidence)
        if not rows:
            store.save('verified',evidence)
            return {'state':'verified','imported':0,'financial_writes':False}
        # XML validation precedes using source identifiers in an Exact filter.
        template=bytes(conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s',('FIBO-20260922-20261002',)).fetchone()[0])
        validation=module.build_xml(rows,template,day,int(str(day.year)[-2:]+journal+'0001'))
        api=module.API(app)
        journals=await api.rows('financial/Journals',{'$filter':"Code eq '"+journal+"'"})
        require(len(journals)==1 and journals[0]['ID']==module.JOURNAL and journals[0]['GLAccount']==module.BANK_GL and journals[0]['Currency']=='EUR' and journals[0]['Type']==12 and journals[0]['IsBlocked'] is False,'journal_changed')
        accounts=await api.rows('crm/Accounts',{'$filter':"Code eq '            "+debtor+"'",'$select':'ID,Code,IsSales,Status'})
        require(len(accounts)==1 and accounts[0]['ID']==module.DEBTOR and accounts[0]['IsSales'] is True and accounts[0]['Status']=='C','debtor_changed')
        ledger=await existing(api,module,rows,day,journal)
        proposed=rows if psp=='fibonatix' else validation[1]
        present,errors=check(module,psp,proposed,ledger)
        require(not errors,'existing_psp_payment_conflict')
        missing=[r for r in rows if r['payment_id'] not in present]
        evidence.update(existing=len(present),before=ledger)
        if not missing:
            store.save('verified',evidence)
            return {'state':'verified','existing':len(present),'imported':0,'financial_writes':False}
        if reconcile_only:
            store.save('uncertain',evidence)
            raise ValueError('import_not_fully_verified_no_retry')
        if psp=='fibonatix' and part=='refunds':
            origins=[]
            for r in missing:
                origins+=await api.rows('financialtransaction/TransactionLines',{'$filter':"JournalCode eq '26' and YourRef eq '"+r['ref']+"'",'$select':module.SELECT})
            module.validate_refund_origins(missing,list({r['ID']:r for r in origins}.values()))
            gl=await api.rows('financial/GLAccounts',{'$filter':"Code eq '1350'",'$select':'ID,Code,BalanceType,Type'})
            require(len(gl)==1 and gl[0]['ID']==module.REFUND_GL and gl[0]['BalanceType']=='B' and gl[0]['Type']==90,'refund_clearing_changed')
        if psp=='icepay' and any(r.get('kind')=='fee' for r in missing):
            gl=await api.rows('financial/GLAccounts',{'$filter':"Code eq '5570'",'$select':'ID,Code,BalanceType,Description'})
            require(len(gl)==1 and gl[0]['ID']==module.FEE_GL and gl[0]['BalanceType']=='W' and gl[0]['Description']=='Payment service provider','fee_ledger_changed')
        entry=await latest_entry(api,day,journal)
        built=module.build_xml(missing,template,day,entry)
        xml,manifest=(built,missing) if psp=='fibonatix' else built
        evidence.update(entry=entry,new_receipts=manifest,xml_sha256=hashlib.sha256(xml).hexdigest(),xml=base64.b64encode(xml).decode())
        require(not task_drain.requested(),'worker_draining')
        token=await app._access_token()
        evidence['routing_before_import']=routing_completion.require_recent(conn,end)
        evidence['write_audit']=fence.audit_metadata()
        store.save('prepared',evidence)
        store.claim(evidence)
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        try:
            async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
                response=await budgeted_http(app,psp,'POST',lambda:client.post(module.BASE+'/docs/XMLUpload.aspx',params={'Topic':'GLTransactions','_Division_':'3977752'},content=xml,headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'}),priority='critical',floor=200)
            evidence['response_status']=response.status_code
        except Exception as exc:
            evidence['upload_error_type']=type(exc).__name__
        store.save('readback_required',evidence)
        after=await api.rows('financialtransaction/TransactionLines',{'$filter':f"JournalCode eq '{journal}' and FinancialYear eq {day.year} and EntryNumber eq {entry}",'$select':module.SELECT})
        present,errors=check(module,psp,manifest,after)
        complete=not errors and len(present)==len(missing) and len(after)==2*len(missing)
        evidence.update(after=after,imported=len(present),readback_verified=complete)
        store.save('verified' if complete else 'uncertain',evidence)
        require(complete,'import_not_fully_verified_no_retry')
        return {'state':'verified','imported':len(present),'existing':evidence['existing'],'financial_writes':True}
    finally:
        if locked:
            conn.execute(lock_sql.replace('pg_try_advisory_lock','pg_advisory_unlock'))
        conn.close()


@fence.owned_operation('fibonatix')
async def fibonatix(app,key,part='receipts',reconcile_only=False):
    return await _run(app,key,'fibonatix',part,reconcile_only)


@fence.owned_operation('icepay')
async def icepay(app,key,part='receipts',reconcile_only=False):
    require(part=='receipts','icepay_refund_adapter_required')
    return await _run(app,key,'icepay',part,reconcile_only)
