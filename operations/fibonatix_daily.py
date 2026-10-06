"""Dated Fibonatix receipt jobs; one fenced upload with durable readback.

Runs only on the assigned Fibonatix worker. No matching and no automatic replay
after an uncertain write. Historical task constants and fixtures are untouched.
"""
import asyncio,base64,copy,hashlib,json,re,time
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from operations import worker_write_fence as fence
from operations.worker_coordination import budgeted_http
from operations import nightly_batches as nightly
from operations.fibonatix_daily_source import prepare,require

DIVISION=3977752
BASE='https://start.exactonline.nl'
TEMPLATE_SHA='18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2'
DEBTOR='ec2af99c-809c-40e3-9057-8a31962ae1cf'
JOURNAL='60f578ce-eb38-43c0-955a-5ab464f84ce3'
BANK_GL='8edb713c-cc61-4f75-a519-67a7a79d2b0e'
SELECT='ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,Currency,Account,AccountCode,GLAccount,GLAccountCode,JournalCode,YourRef,PaymentReference,FinancialYear,FinancialPeriod'

def exact_date(value):
    value=str(value or '')
    return datetime.fromtimestamp(int(re.search(r'-?\d+',value).group())/1000,timezone.utc).date().isoformat() if value.startswith('/Date(') else value[:10]

def validate(job):
    require(set(job['params'])=={'date'},'invalid_daily_params')
    day=date.fromisoformat(job['params']['date'])
    require(job['task_key']==nightly.identity(day,'fibonatix') and job['action'] in {'daily_prepare','daily_import','daily_reconcile'},'invalid_daily_job')
    require(day<datetime.now(nightly.ZONE).date(),'day_not_closed')
    return day

def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_fibonatix_daily_imports(
        division integer NOT NULL,processing_date date NOT NULL,
        state text NOT NULL,write_requested boolean NOT NULL DEFAULT false,
        data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(division,processing_date))""")

def description(r):
    return 'Fibonatix '+r['ref']+' | Woo '+str(r['woo_id'])+' | Betaling '+r['payment_id']

def reconcile(rows,ledger):
    existing=[];missing=[];errors=[]
    for r in rows:
        pid=r['payment_id']
        hits=[l for l in ledger if pid in set(re.findall(r'\b[A-Za-z0-9]+\b',str(l.get('Description') or '')+' '+str(l.get('PaymentReference') or '')))]
        if not hits:
            missing.append(pid);continue
        bank=[l for l in hits if str(l.get('GLAccountCode','')).strip()=='1316']
        offset=[l for l in hits if str(l.get('GLAccountCode','')).strip()=='1100']
        ok=len(hits)==2 and len(bank)==len(offset)==1
        if ok:
            ok=(Decimal(str(bank[0]['AmountDC']))==Decimal(r['amount'])
                and Decimal(str(offset[0]['AmountDC']))==-Decimal(r['amount'])
                and offset[0].get('Account')==DEBTOR
                and str(offset[0].get('AccountCode','')).strip()=='100100'
                and bank[0]['EntryID']==offset[0]['EntryID']
                and offset[0].get('YourRef')==r['ref']
                and all(str(l.get('JournalCode','')).strip()=='26' and l.get('Currency')=='EUR'
                    and exact_date(l['Date'])==r['date'] and l.get('Description')==description(r) for l in hits))
        (existing if ok else errors).append(pid)
    duplicate_orders=[]
    for r in rows:
        if any(str(l.get('GLAccountCode','')).strip()=='1316'
               and re.search(r'\b'+re.escape(r['ref'])+r'\b',str(l.get('Description') or '')+' '+str(l.get('YourRef') or ''))
               and r['payment_id'] not in set(re.findall(r'\b[A-Za-z0-9]+\b',str(l.get('Description') or '')+' '+str(l.get('PaymentReference') or ''))) for l in ledger):
            duplicate_orders.append(r['payment_id'])
    return dict(existing=existing,missing=missing,errors=errors,other_receipts_same_order=duplicate_orders)

def build_xml(rows,template,day,entry):
    require(hashlib.sha256(template).hexdigest()==TEMPLATE_SHA,'template_changed')
    require(rows and len({r['payment_id'] for r in rows})==len(rows),'invalid_receipt_set')
    require(type(entry) is int and entry>0,'invalid_entry_number')
    old=ET.fromstring(template)
    et,lt=next((e,l) for e in old.findall('./GLTransactions/GLTransaction') for l in e.findall('GLTransactionLine') if l.find('GLAccount').get('code')=='1100' and l.find('Account') is not None and l.find('Account').get('code')=='100100' and Decimal(l.findtext('Amount/Value'))>0)
    require([c.tag for c in et if c.tag!='GLTransactionLine']==['TransactionType','Journal','Date','FinYear','FinPeriod','Description'],'header_schema_changed')
    require([c.tag for c in lt]==['Date','VATType','FinYear','FinPeriod','GLAccount','Description','Account','Amount','References','Note'],'line_schema_changed')
    root=ET.Element('eExact');txs=ET.SubElement(root,'GLTransactions');e=ET.SubElement(txs,'GLTransaction',entry=str(entry))
    for child in et:
        if child.tag!='GLTransactionLine':e.append(copy.deepcopy(child))
    e.find('Journal').set('code','26');e.find('Date').text=day.isoformat()
    e.find('FinYear').set('number',str(day.year));e.find('FinPeriod').set('number',str(day.month))
    e.find('Description').text=nightly.identity(day,'fibonatix')
    for number,r in enumerate(sorted(rows,key=lambda x:x['payment_id']),1):
        require(r['date']==day.isoformat() and r['order_status']=='completed' and re.fullmatch('[A-Za-z0-9]{6,32}',r['payment_id']) and re.fullmatch('TD[0-9]+',r['ref']) and type(r['woo_id']) is int and Decimal(r['amount'])>0,'invalid_receipt')
        l=copy.deepcopy(lt);l.set('line',str(number));l.find('Date').text=day.isoformat()
        l.find('FinYear').set('number',str(day.year));l.find('FinPeriod').set('number',str(day.month))
        l.find('GLAccount').set('code','1100');l.find('Account').set('code','100100');l.find('Description').text=description(r)
        l.find('Amount/Currency').set('code','EUR');l.find('Amount/Value').text=r['amount']
        l.find('References/PaymentReference').text=r['payment_id'];l.find('References/YourRef').text=r['ref']
        l.find('Note').text=nightly.identity(day,'fibonatix')+' | '+description(r);e.append(l)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)
class API:
    def __init__(self,app):
        require(app.DIVISION==DIVISION and app.BASE_URL==BASE,'wrong_administration')
        self.app=app;self.calls=0;self.quota={};self.last=0
    async def get(self,resource,params=None,url=None):
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        require(resource in {'financial/Journals','crm/Accounts','financial/GLAccounts','financialtransaction/TransactionLines','read/financial/ReceivablesList'},'invalid_read_resource')
        url=url or f'{BASE}/api/v1/{DIVISION}/{resource}';u=urlsplit(url)
        require(u.scheme=='https' and u.netloc=='start.exactonline.nl' and u.path==f'/api/v1/{DIVISION}/{resource}' and not u.fragment and self.calls<90,'read_scope_or_budget')
        await asyncio.sleep(max(0,2-(time.monotonic()-self.last)))
        token=await self.app._access_token();self.calls+=1;self.last=time.monotonic()
        async with httpx.AsyncClient(timeout=45,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as c:
            response=await budgeted_http(self.app,'fibonatix','GET',lambda:c.get(url,params=params,headers={'Authorization':'Bearer '+token,'Accept':'application/json'}),floor=200)
        self.quota={k:int(response.headers[h]) for k,h in [('daily','x-ratelimit-remaining'),('minute','x-ratelimit-minutely-remaining')] if response.headers.get(h,'').isdigit()}
        require(response.status_code==200,'exact_http_'+str(response.status_code))
        require(self.quota.get('daily',0)>200 and self.quota.get('minute',0)>5,'api_budget_low_or_unknown')
        return response.json()
    async def rows(self,resource,params):
        rows=[];seen=set();url=None
        while True:
            raw=await self.get(resource,params,url);d=raw.get('d',{});batch=d.get('results') if isinstance(d,dict) else d
            require(isinstance(batch,list),'invalid_response');rows+=batch;require(len(rows)<=6000,'row_limit')
            url=raw.get('__next') or (d.get('__next') if isinstance(d,dict) else None)
            if not url:return rows
            require(url not in seen and bool(batch),'bad_pagination');seen.add(url);params=None

async def ledger(api,rows,day):
    actual=await api.rows('financialtransaction/TransactionLines',{'$filter':"JournalCode eq '26' and FinancialYear eq "+str(day.year),'$select':SELECT,'$orderby':'EntryNumber,LineNumber'})
    for i in range(0,len(rows),12):
        query=' or '.join("PaymentReference eq '"+r['payment_id']+"'" for r in rows[i:i+12])
        actual+=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':SELECT})
    return list({r['ID']:r for r in actual}.values())

def persist(conn,day,state,data):
    conn.execute('UPDATE jnp_fibonatix_daily_imports SET state=%s,data=%s::jsonb,updated_at=now() WHERE division=%s AND processing_date=%s',(state,json.dumps(data),DIVISION,day))

def source(conn,day):
    key=nightly.identity(day,'fibonatix')+':source'
    row=conn.execute('SELECT result FROM paragon_login_probes WHERE probe_id=%s',(key,)).fetchone()
    require(row and row[0].get('source_window_verified') and row[0]['source_timestamp_timezone']=='UTC','source_not_verified')
    src=row[0]
    raw=base64.b64decode(src['source_csv'],validate=True)
    require(hashlib.sha256(raw).hexdigest()==src['source_sha256'],'source_changed')
    from operations.fibonatix_daily_source import bounds
    lo,hi=bounds(day)
    require(src['dates']==[lo.strftime('%d/%m/%Y'),(hi-timedelta(microseconds=1)).strftime('%d/%m/%Y')],'source_filter_does_not_cover_day')
    counts=re.findall(r'Showing [0-9,]+[^0-9]+[0-9,]+ of ([0-9,]+) results',src['table_snapshot'])
    require(len(counts)==1 and int(counts[0].replace(',',''))==src['source_rows'],'source_export_count_mismatch')
    orders=conn.execute('SELECT result FROM paragon_login_probes WHERE probe_id=%s',(key+':orders',)).fetchone()
    require(orders and orders[0]['state']=='read_complete' and not orders[0]['missing_ids'],'order_evidence_incomplete')
    require((datetime.now(timezone.utc)-datetime.fromisoformat(orders[0]['captured_at'])).total_seconds()<3600,'order_evidence_stale')
    rows,exceptions,summary=prepare(raw,orders[0]['orders'],day,utc_ui_proof=src['utc_ui_proof'])
    return rows,exceptions,summary,src['source_sha256']

@fence.owned_operation('fibonatix')
async def upload(app,conn,api,day,data):
    from operations import task_drain
    require(not task_drain.requested(),'worker_draining')
    xml=base64.b64decode(data['xml'],validate=True)
    require(hashlib.sha256(xml).hexdigest()==data['xml_sha256'],'xml_changed')
    token=await app._access_token()
    claimed=conn.execute("""UPDATE jnp_fibonatix_daily_imports SET write_requested=true,state='upload_requested',updated_at=now()
        WHERE division=%s AND processing_date=%s AND state='prepared' AND write_requested=false RETURNING processing_date""",(DIVISION,day)).fetchone()
    require(bool(claimed),'previous_write_reconcile_only')
    data['write_audit']=fence.audit_metadata()
    persist(conn,day,'upload_requested',data)
    import httpx
    from operations.bacs_debtor_transfer import TLS_CONTEXT
    try:
        async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
            response=await budgeted_http(app,'fibonatix','POST',lambda:client.post(BASE+'/docs/XMLUpload.aspx',params={'Topic':'GLTransactions','_Division_':str(DIVISION)},content=xml,headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'}),priority='critical',floor=200)
        data['response_status']=response.status_code
        try:
            parsed=ET.fromstring(response.content)
            data['response']=ET.tostring(parsed,encoding='unicode') if parsed.tag in {'eExact','Messages','Message'} else 'unexpected_document'
        except ET.ParseError:
            data['response']='unparsed_response'
    except Exception as exc:
        data['upload_error_type']=type(exc).__name__
    persist(conn,day,'readback_required',data)
    actual=await ledger(api,data['candidates'],day)
    check=reconcile(data['candidates'],actual)
    data.update(after=actual,after_check=check,api_calls=api.calls)
    complete=not check['missing'] and not check['errors'] and not check['other_receipts_same_order']
    persist(conn,day,'verified' if complete else 'uncertain',data)
    require(complete,'upload_unverified_no_retry')
    return {'state':'verified','imported':len(data['new_receipts']),'existing':len(data['before_check']['existing']),'exceptions':data['summary']['exceptions'],'financial_writes':True}

async def run(app,job):
    day=validate(job)
    require(fence.current_owner() is not None and fence.current_owner().role=='fibonatix','assigned_fibonatix_worker_required')
    require(app.DIVISION==DIVISION and app.BASE_URL==BASE,'wrong_administration')
    conn=app._db_connect();locked=False
    try:
        locked=conn.execute('SELECT pg_try_advisory_lock(397775226)').fetchone()[0]
        require(locked,'another_fibonatix_writer')
        initialize(conn)
        registered=conn.execute('SELECT window_start,window_end FROM jnp_nightly_runs WHERE division=%s AND processing_date=%s',(DIVISION,day)).fetchone()
        from operations.fibonatix_daily_source import bounds
        require(registered and tuple(registered)==bounds(day),'day_not_registered')
        prior=conn.execute('SELECT state,write_requested,data FROM jnp_fibonatix_daily_imports WHERE division=%s AND processing_date=%s',(DIVISION,day)).fetchone()
        if prior and prior[0]=='verified':
            return {'state':'verified','already_completed':True,'financial_writes':False}
        if prior and prior[1] and job['action']!='daily_reconcile':
            raise ValueError('previous_write_reconcile_only')
        candidates,exceptions,summary,sha=source(conn,day)
        if not prior:
            require(job['action']=='daily_prepare','prepare_required')
            data=dict(candidates=candidates,exceptions=exceptions,summary=summary,source_sha256=sha)
            conn.execute("INSERT INTO jnp_fibonatix_daily_imports(division,processing_date,state,data) VALUES(%s,%s,'preparing',%s::jsonb)",(DIVISION,day,json.dumps(data)))
        else:
            data=prior[2]
            require(data['candidates']==candidates and data['source_sha256']==sha,'prepared_evidence_changed')
            if prior[0]=='prepared' and job['action']=='daily_prepare':
                return {'state':'prepared','already_prepared':True,'financial_writes':False}
        api=API(app)
        journals=await api.rows('financial/Journals',{'$filter':"Code eq '26'"})
        require(len(journals)==1 and journals[0]['ID']==JOURNAL and journals[0]['GLAccount']==BANK_GL and journals[0]['Type']==12 and journals[0]['Currency']=='EUR' and journals[0]['IsBlocked'] is False,'journal_changed')
        accounts=await api.rows('crm/Accounts',{'$filter':"Code eq '            100100'",'$select':'ID,Code,IsSales,Status'})
        require(len(accounts)==1 and accounts[0]['ID']==DEBTOR and accounts[0]['IsSales'] is True and accounts[0]['Status']=='C','debtor_changed')
        actual=await ledger(api,candidates,day)
        check=reconcile(candidates,actual)
        data.update(latest_check=check,api_calls=api.calls)
        if not check['missing'] and not check['errors'] and not check['other_receipts_same_order']:
            data['verified_ledger']=actual
            persist(conn,day,'verified',data)
            return {'state':'verified','imported':0,'existing':len(check['existing']),'exceptions':summary['exceptions'],'financial_writes':False}
        require(not check['errors'] and not check['other_receipts_same_order'],'existing_or_ambiguous_receipt')
        if job['action']=='daily_reconcile':
            data['reconciliation']=actual
            persist(conn,day,'uncertain' if prior and prior[1] else 'blocked',data)
            return {'state':'requires_review','financial_writes':False}
        if job['action']=='daily_prepare':
            # Sequence is derived from the current, fully read existing journal.
            entries={int(l['EntryNumber']) for l in actual if str(l.get('JournalCode','')).strip()=='26' and l.get('FinancialYear')==day.year}
            require(bool(entries),'entry_sequence_missing')
            entry=max(entries)+1
            require(str(entry).startswith(str(day.year)[-2:]+'26'),'unexpected_entry_sequence')
            new=[r for r in candidates if r['payment_id'] in check['missing']]
            template=bytes(conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s',('FIBO-20260922-20261002',)).fetchone()[0])
            xml=build_xml(new,template,day,entry)
            data.update(before=actual,before_check=check,new_receipts=new,entry=entry,xml=base64.b64encode(xml).decode(),xml_sha256=hashlib.sha256(xml).hexdigest())
            persist(conn,day,'prepared',data)
            return {'state':'prepared','receipts':len(new),'existing':len(check['existing']),'summary':summary,'financial_writes':False}
        require(prior and prior[0]=='prepared' and check==data['before_check'],'preflight_changed')
        require(not any(l['EntryNumber']==data['entry'] and str(l.get('JournalCode','')).strip()=='26' for l in actual),'entry_now_occupied')
        return await upload(app,conn,api,day,data)
    finally:
        if locked:conn.execute('SELECT pg_advisory_unlock(397775226)')
        conn.close()
