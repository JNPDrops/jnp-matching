"""Calendar-day ICEPAY imports with immutable source evidence and one fenced write.

No order-status filter, matching, historical activation or replay of unknown writes.
"""
import asyncio,base64,copy,csv,hashlib,io,json,re,time
from datetime import date,datetime,timezone
from decimal import Decimal
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from operations import worker_write_fence as fence, nightly_batches as nightly
from operations.worker_coordination import budgeted_http
TEMPLATE_SHA='18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2'
FEE_GL='6005c8a5-a475-44a4-9ee2-59f6ac987919'
FEE_CODE='5570'
BASE='https://start.exactonline.nl';DIVISION=3977752
DEBTOR='0492e907-6698-4281-98e5-c46e01ae9219'
JOURNAL='07da1219-d8d8-4f72-b4f8-3d6735d6e65a';BANK_GL='9cb20757-80d9-4199-b2ff-055562855e5e'
SELECT='ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,Currency,Account,AccountCode,GLAccount,GLAccountCode,JournalCode,YourRef,PaymentReference,FinancialYear,FinancialPeriod'

def require(ok,reason):
    if not ok:raise ValueError(reason)

def classify_rows(raw, rows):
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')),delimiter=';')
    original={}
    for row in reader:
        n={re.sub(r'[^a-z0-9]','',k.lower()):v.strip() for k,v in row.items()}
        original[n['paymentid']]=n
    result=[]
    for r in rows:
        if r['status']!='OK':continue
        value=Decimal(r['amount'])
        if value>0:
            require(bool(re.fullmatch(r'\d+',str(r.get('order') or ''))),'receipt_order_missing')
            result.append({**r,'kind':'receipt'})
        else:
            n=original[r['payment_id']]
            match=re.fullmatch(r'PID (\d+) EUR (\d+\.\d{2}) \| (?:MC|VISA) \| ICE BlendRate (\d+\.\d{2})%',n.get('description',''))
            require(value<0 and r['order'] is None and re.fullmatch(r'\d+Costs',n.get('reference','')) and match is not None,'unrecognized_nonreceipt')
            require((Decimal(match[2])*Decimal(match[3])/100).quantize(Decimal('0.01'))==-value,'fee_amount_mismatch')
            result.append({**r,'kind':'fee','cost_reference':n['reference'],'cost_description':n['description']})
    return result


def build_xml(rows,template,day,entry):
    DAY=day.isoformat();ENTRY=entry;JOB=nightly.identity(day,'icepay')
    require(type(entry) is int and entry>0,'invalid_entry_number')
    require(hashlib.sha256(template).hexdigest()==TEMPLATE_SHA,'template_changed')
    require(0<len(rows)<=5000 and len({r['payment_id'] for r in rows})==len(rows),'source_count_changed')
    for r in rows:
        require(r['status']=='OK' and r['merchant']=='34950' and r['date']==DAY and re.fullmatch(r'\d+',r['payment_id']) and ((r.get('kind','receipt')=='receipt' and re.fullmatch(r'\d+',str(r.get('order') or '')) and Decimal(r['amount'])>0) or (r.get('kind')=='fee' and r.get('order') is None and Decimal(r['amount'])<0 and re.fullmatch(r'\d+Costs',r.get('cost_reference','')))),'invalid_source')
    original=ET.fromstring(template)
    candidates=[(e,l) for e in original.findall('./GLTransactions/GLTransaction') for l in e.findall('GLTransactionLine') if l.find('GLAccount').get('code')=='1100' and l.find('Account') is not None and l.find('Account').get('code')=='100100' and Decimal(l.findtext('Amount/Value'))>0]
    require(bool(candidates),'receipt_template_missing')
    et,lt=candidates[0]
    require([c.tag for c in et if c.tag!='GLTransactionLine']==['TransactionType','Journal','Date','FinYear','FinPeriod','Description'],'header_schema_changed')
    require([c.tag for c in lt]==['Date','VATType','FinYear','FinPeriod','GLAccount','Description','Account','Amount','References','Note'],'line_schema_changed')
    root=ET.Element('eExact');txs=ET.SubElement(root,'GLTransactions');manifest=[]
    for day,(count,total,number) in {DAY:(len(rows),sum((Decimal(r['amount']) for r in rows),Decimal('0.00')),ENTRY)}.items():
        selected=sorted([r for r in rows if r['date']==day],key=lambda r:r['payment_id'])
        require(len(selected)==count and sum((Decimal(r['amount']) for r in selected),Decimal(0))==total,'day_total_changed')
        e=ET.SubElement(txs,'GLTransaction',entry=str(number))
        for child in et:
            if child.tag!='GLTransactionLine':e.append(copy.deepcopy(child))
        e.find('Journal').set('code','27');e.find('Date').text=day
        e.find('FinYear').set('number',str(date.fromisoformat(day).year));e.find('FinPeriod').set('number',str(date.fromisoformat(day).month));e.find('Description').text=JOB+' '+day
        for n,r in enumerate(selected,1):
            l=copy.deepcopy(lt);l.set('line',str(n));l.find('Date').text=day
            l.find('FinYear').set('number',str(date.fromisoformat(day).year));l.find('FinPeriod').set('number',str(date.fromisoformat(day).month))
            l.find('GLAccount').set('code','1100');l.find('Account').set('code','109419')
            if r.get('kind','receipt')=='receipt':
                description='ICEPAY TD'+r['order']+' | Payment '+r['payment_id'];ref='TD'+r['order']
            else:
                l.find('GLAccount').set('code',FEE_CODE);l.remove(l.find('Account'))
                description='ICEPAY Costs | Payment '+r['payment_id'];ref=r['cost_reference']
            l.find('Description').text=description;l.find('Amount/Currency').set('code','EUR');l.find('Amount/Value').text=r['amount']
            l.find('References/PaymentReference').text=r['payment_id'];l.find('References/YourRef').text=ref
            l.find('Note').text=JOB+' | '+description+(' | '+r['cost_description'] if r.get('kind')=='fee' else '');e.append(l)
            manifest.append({**r,'entry':number,'description':description,'ref':ref})
    return ET.tostring(root,encoding='utf-8',xml_declaration=True),manifest

def exact_date(value):
    value=str(value or '')
    return datetime.fromtimestamp(int(re.search(r'-?\d+',value).group())/1000,timezone.utc).date().isoformat() if value.startswith('/Date(') else value[:10]

def reconcile(manifest,ledger):
    hits={r['payment_id']:[] for r in manifest};verified=[];errors=[];possible=[]
    for line in ledger:
        tokens=set(re.findall(r'\b\d+\b',' '.join(str(line.get(k) or '') for k in ['Description','PaymentReference'])))
        for key in hits.keys()&tokens:hits[key].append(line)
    for r in manifest:
        found=hits[r['payment_id']]
        if not found:continue
        bank=[l for l in found if str(l.get('GLAccountCode','')).strip()=='1317']
        fee=r.get('kind')=='fee'
        offset=[l for l in found if str(l.get('GLAccountCode','')).strip()==(FEE_CODE if fee else '1100')]
        valid=len(found)==2 and len(bank)==1 and len(offset)==1
        if valid:
            valid=(Decimal(str(bank[0]['AmountDC']))==Decimal(r['amount']) and Decimal(str(offset[0]['AmountDC']))==-Decimal(r['amount']) and ((fee and offset[0].get('Account') is None and offset[0].get('GLAccount')==FEE_GL) or (not fee and str(offset[0].get('AccountCode','')).strip()=='109419' and offset[0].get('Account')==DEBTOR)) and bank[0]['EntryID']==offset[0]['EntryID'])
            valid=valid and all(str(l.get('JournalCode','')).strip()=='27' and l['EntryNumber']==r['entry'] and l['Description']==r['description'] and l.get('Currency')=='EUR' and exact_date(l['Date'])==r['date'] for l in found) and offset[0].get('YourRef')==r['ref']
        (verified if valid else errors).append(r['payment_id'])
    occupied=sorted({l['EntryNumber'] for l in ledger if str(l.get('JournalCode','')).strip()=='27'}&{r['entry'] for r in manifest})
    for r in manifest:
        for l in ledger:
            if str(l.get('GLAccountCode','')).strip()!='1317':continue
            if re.search(r'\b'+re.escape(r['ref'])+r'\b',str(l.get('Description') or '')+' '+str(l.get('YourRef') or '')) and l not in hits[r['payment_id']]:possible.append(r['payment_id'])
    return {'verified_ids':verified,'existing_ids':[k for k,v in hits.items() if v],'errors':errors,'occupied_entries':occupied,'other_receipts_same_order':sorted(set(possible)),'complete':len(verified)==len(manifest) and not errors,'safe_to_import':not any(hits.values()) and not occupied and not possible}

class API:
    def __init__(self,app):
        require(app.DIVISION==DIVISION and app.BASE_URL==BASE,'wrong_administration')
        self.app=app;self.calls=0;self.quota={};self.last=0
    async def get(self,resource,params=None,url=None):
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        require(resource in {'financial/Journals','crm/Accounts','financial/GLAccounts','financialtransaction/TransactionLines','read/financial/ReceivablesList'},'invalid_read_resource')
        url=url or f'{BASE}/api/v1/{DIVISION}/{resource}';u=urlsplit(url)
        require(u.scheme=='https' and u.netloc=='start.exactonline.nl' and u.path==f'/api/v1/{DIVISION}/{resource}' and not u.fragment and self.calls<45,'read_scope_or_budget')
        await asyncio.sleep(max(0,2-(time.monotonic()-self.last)))
        token=await self.app._access_token();self.calls+=1;self.last=time.monotonic()
        async with httpx.AsyncClient(timeout=45,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as c:
            response=await budgeted_http(self.app,'icepay','GET',lambda:c.get(url,params=params,headers={'Authorization':'Bearer '+token,'Accept':'application/json'}),floor=200)
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
    async def ledger(self,day):
        return await self.rows('financialtransaction/TransactionLines',{'$filter':f"FinancialYear eq {day.year} and (JournalCode eq '27' or GLAccount eq guid'{BANK_GL}' or Account eq guid'{DEBTOR}' or GLAccount eq guid'{FEE_GL}')",'$select':SELECT,'$orderby':'EntryNumber,LineNumber'})


def validate(job):
    require(job['action'] in {'daily_source','daily_prepare','daily_import','daily_reconcile'} and set(job['params'])=={'date'},'invalid_daily_job')
    day=date.fromisoformat(job['params']['date'])
    require(day<datetime.now(nightly.ZONE).date() and job['task_key']==nightly.identity(day,'icepay'),'invalid_daily_identity')
    return day


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_icepay_daily_imports(
        division integer NOT NULL,processing_date date NOT NULL,state text NOT NULL,
        write_requested boolean NOT NULL DEFAULT false,data jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(division,processing_date))''')


def source(conn,day):
    key=nightly.identity(day,'icepay')+':source'
    row=conn.execute('SELECT status,artifacts,summary FROM icepay_transaction_tasks WHERE job=%s',(key,)).fetchone()
    require(row and row[0].get('state')=='downloaded','source_not_downloaded')
    artifacts,summary=row[1:]
    require(artifacts.get('refunds',{}).get('total')==0,'refunds_require_separate_processing')
    proof=artifacts['proof']
    require(proof['period_from']==proof['period_through']==day.isoformat(),'source_period_changed')
    if summary.get('source_rows')==0:
        require(proof['ui_payment_ids']==[] and summary.get('td_ok_count')==0,'empty_source_unverified')
        return [],summary,None
    raw=base64.b64decode(artifacts['payments_csv'],validate=True)
    require(hashlib.sha256(raw).hexdigest()==summary['source_sha256'],'source_hash_changed')
    from operations.icepay_source_window import validate_source
    rows,checked,_=validate_source(raw,proof['ui_payment_ids'],artifacts['table_evidence'],day,day)
    require(rows==artifacts['transactions'],'source_rows_changed')
    candidates=classify_rows(raw,rows)
    require(len(candidates)==checked['td_ok_count'] and sum((Decimal(r['amount']) for r in candidates),Decimal(0))==Decimal(checked['td_ok_total']),'source_total_changed')
    return candidates,checked,summary['source_sha256']


def manifest_for_existing(manifest,ledger):
    result=[]
    for r in manifest:
        hits=[l for l in ledger if r['payment_id'] in set(re.findall(r'\b\d+\b',' '.join(str(l.get(k) or '') for k in ('Description','PaymentReference'))))]
        entries={l['EntryNumber'] for l in hits}
        require(len(entries)<=1,'payment_id_multiple_entries')
        result.append({**r,'entry':next(iter(entries)) if entries else r['entry']})
    return result


@fence.owned_operation('icepay')
async def run(app,job):
    day=validate(job)
    require(app.DIVISION==DIVISION and fence.current_owner() is not None and fence.current_owner().role=='icepay','assigned_icepay_worker_required')
    with app._db_connect() as conn:
        from operations.icepay_daily_source import validate_window
        validate_window(day,conn.execute('SELECT window_start,window_end FROM jnp_nightly_runs WHERE division=%s AND processing_date=%s',(DIVISION,day)).fetchone())
    if job['action']=='daily_source':
        from operations.icepay_daily_source import capture
        result=await capture(app,day)
        require(result['status']['state']=='downloaded','source_capture_incomplete')
        return result
    conn=app._db_connect();locked=False
    def save(state,data):
        conn.execute('UPDATE jnp_icepay_daily_imports SET state=%s,data=%s::jsonb,updated_at=now() WHERE division=%s AND processing_date=%s',(state,json.dumps(data),DIVISION,day))
    try:
        locked=conn.execute("SELECT pg_try_advisory_lock(hashtextextended('jnp:3977752:icepay:receipts',0))").fetchone()[0]
        require(locked,'another_icepay_writer')
        initialize(conn)
        prior=conn.execute('SELECT state,write_requested,data FROM jnp_icepay_daily_imports WHERE division=%s AND processing_date=%s',(DIVISION,day)).fetchone()
        if prior and prior[0]=='verified':return {'state':'verified','already_completed':True}
        require(not prior or not prior[1] or job['action']=='daily_reconcile','previous_write_reconcile_only')
        candidates,summary,sha=source(conn,day)
        if prior:
            data=prior[2]
            require(data['candidates']==candidates and data['source_sha256']==sha,'source_changed_after_prepare')
        else:
            require(job['action']=='daily_prepare','prepare_required')
            data={'candidates':candidates,'summary':summary,'source_sha256':sha}
            conn.execute("INSERT INTO jnp_icepay_daily_imports(division,processing_date,state,data) VALUES(%s,%s,'preparing',%s::jsonb)",(DIVISION,day,json.dumps(data)))
        if not candidates:
            data.update(imported=0,existing=0,financial_writes=False);save('verified',data);return {'state':'verified','imported':0}
        api=API(app)
        journals=await api.rows('financial/Journals',{'$filter':"Code eq '27'"})
        require(len(journals)==1 and journals[0]['ID']==JOURNAL and journals[0]['GLAccount']==BANK_GL and journals[0]['Currency']=='EUR' and journals[0]['Type']==12 and journals[0]['IsBlocked'] is False,'journal_changed')
        accounts=await api.rows('crm/Accounts',{'$filter':"Code eq '            109419'",'$select':'ID,Code,IsSales,Status'})
        require(len(accounts)==1 and accounts[0]['ID']==DEBTOR and accounts[0]['IsSales'] is True and accounts[0]['Status']=='C','debtor_changed')
        fees=await api.rows('financial/GLAccounts',{'$filter':"Code eq '5570'",'$select':'ID,Code,Description,BalanceType'})
        require(len(fees)==1 and fees[0]['ID']==FEE_GL and fees[0]['BalanceType']=='W' and fees[0]['Description']=='Payment service provider','fee_ledger_changed')
        actual=await api.ledger(day)
        for i in range(0,len(candidates),12):
            query=' or '.join("PaymentReference eq '"+r['payment_id']+"'" for r in candidates[i:i+12])
            actual+=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':SELECT})
        actual=list({l['ID']:l for l in actual}.values())
        template=bytes(conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s',('FIBO-20260922-20261002',)).fetchone()[0])
        entries={int(l['EntryNumber']) for l in actual if str(l.get('JournalCode','')).strip()=='27' and l.get('FinancialYear')==day.year}
        require(bool(entries),'entry_sequence_missing')
        entry=data.get('entry',max(entries)+1)
        require(str(entry).startswith(str(day.year)[-2:]+'27'),'entry_sequence_unexpected')
        _,proposed=build_xml(candidates,template,day,entry)
        manifest=manifest_for_existing(proposed,actual) if not prior or not prior[1] else data['manifest']
        check=reconcile(manifest,actual)
        data.update(latest_check=check,api_calls=api.calls)
        require(not check['errors'] and not check['other_receipts_same_order'],'existing_or_ambiguous_receipt')
        if check['complete']:
            data.update(verified_ledger=actual,existing=len(manifest));save('verified',data);return {'state':'verified','existing':len(manifest)}
        if job['action']=='daily_reconcile':
            save('uncertain' if prior and prior[1] else 'blocked',data);raise ValueError('receipt_readback_incomplete')
        missing=[r for r in candidates if r['payment_id'] not in check['verified_ids']]
        require(entry not in entries,'entry_now_occupied')
        xml,new_manifest=build_xml(missing,template,day,entry)
        if job['action']=='daily_prepare':
            data.update(entry=entry,manifest=manifest,new_manifest=new_manifest,before_check=check,xml=base64.b64encode(xml).decode(),xml_sha256=hashlib.sha256(xml).hexdigest(),before=actual)
            save('prepared',data);return {'state':'prepared','new_transactions':len(missing)}
        require(prior and prior[0]=='prepared' and check==data['before_check'] and data['new_manifest']==new_manifest and data['xml_sha256']==hashlib.sha256(xml).hexdigest(),'prepared_evidence_changed')
        from operations import task_drain
        require(not task_drain.requested(),'worker_draining')
        token=await app._access_token()
        require(api.quota.get('daily',0)>200 and api.quota.get('minute',0)>8,'write_budget_low')
        from operations import routing_completion
        from operations.fibonatix_daily_source import bounds
        data['routing_before_import']=routing_completion.require_recent(conn,bounds(day)[1])
        require(conn.execute("UPDATE jnp_icepay_daily_imports SET write_requested=true,state='upload_requested',updated_at=now() WHERE division=%s AND processing_date=%s AND state='prepared' AND write_requested=false RETURNING division",(DIVISION,day)).fetchone(),'write_already_requested')
        data['write_audit']=fence.audit_metadata();save('upload_requested',data)
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        try:
            async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
                response=await budgeted_http(app,'icepay','POST',lambda:client.post(BASE+'/docs/XMLUpload.aspx',params={'Topic':'GLTransactions','_Division_':str(DIVISION)},content=xml,headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'}),priority='critical',floor=200)
            data['response_status']=response.status_code
        except Exception as exc:data['upload_error_type']=type(exc).__name__
        save('readback_required',data)
        after=await api.rows('financialtransaction/TransactionLines',{'$filter':"JournalCode eq '27' and FinancialYear eq "+str(day.year)+' and EntryNumber eq '+str(entry),'$select':SELECT,'$orderby':'LineNumber'})
        after_check=reconcile(new_manifest,after);data.update(after=after,after_check=after_check,imported=len(new_manifest))
        complete=after_check['complete'] and len(after)==2*len(new_manifest)
        save('verified' if complete else 'uncertain',data)
        require(complete,'upload_unverified_no_retry')
        return {'state':'verified','imported':len(new_manifest)}
    finally:
        if locked:conn.execute("SELECT pg_advisory_unlock(hashtextextended('jnp:3977752:icepay:receipts',0))")
        conn.close()

