"""Explicit 6 October ICEPAY receipts: prepare, one upload, then readback.

No payouts, fees, account creation, old task activation or automatic write retry.
Credentials remain inside the existing Allocation integration and its PG lock.
"""
import asyncio,base64,copy,hashlib,json,re,time
from datetime import date,datetime,timezone
from decimal import Decimal
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

JOB='ICEPAY-20261006'
TASK='icepay-process-20261006-v1'
EXPIRES=datetime(2026,10,7,22,tzinfo=timezone.utc)
from operations import worker_write_fence as fence
from operations.worker_coordination import budgeted_http
SOURCE='icepay-source-20261006-20261006-v1'
TEMPLATE_SHA='18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2'
DAY='2026-10-06'
ENTRY=26270006
BASE='https://start.exactonline.nl';DIVISION=3977752
DEBTOR='0492e907-6698-4281-98e5-c46e01ae9219'
JOURNAL='07da1219-d8d8-4f72-b4f8-3d6735d6e65a';BANK_GL='9cb20757-80d9-4199-b2ff-055562855e5e'
SELECT='ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,Currency,Account,AccountCode,GLAccount,GLAccountCode,JournalCode,YourRef,PaymentReference,FinancialYear,FinancialPeriod'

def require(ok,reason):
    if not ok:raise ValueError(reason)

def build_xml(rows,template):
    require(hashlib.sha256(template).hexdigest()==TEMPLATE_SHA,'template_changed')
    require(0<len(rows)<=5000 and len({r['payment_id'] for r in rows})==len(rows),'source_count_changed')
    for r in rows:
        require(r['status']=='OK' and r['merchant']=='34950' and r['date']==DAY and re.fullmatch(r'\d+',r['payment_id']) and re.fullmatch(r'\d+',str(r.get('order') or '')) and Decimal(r['amount'])>0,'invalid_source')
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
        e.find('FinYear').set('number','2026');e.find('FinPeriod').set('number','10');e.find('Description').text=JOB+' '+day
        for n,r in enumerate(selected,1):
            l=copy.deepcopy(lt);l.set('line',str(n));l.find('Date').text=day
            l.find('FinYear').set('number','2026');l.find('FinPeriod').set('number','10')
            l.find('GLAccount').set('code','1100');l.find('Account').set('code','109419')
            description='ICEPAY TD'+r['order']+' | Payment '+r['payment_id'];ref='TD'+r['order']
            l.find('Description').text=description;l.find('Amount/Currency').set('code','EUR');l.find('Amount/Value').text=r['amount']
            l.find('References/PaymentReference').text=r['payment_id'];l.find('References/YourRef').text=ref
            l.find('Note').text=JOB+' | '+description;e.append(l)
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
        offset=[l for l in found if str(l.get('GLAccountCode','')).strip()=='1100']
        valid=len(found)==2 and len(bank)==1 and len(offset)==1
        if valid:
            valid=(Decimal(str(bank[0]['AmountDC']))==Decimal(r['amount']) and Decimal(str(offset[0]['AmountDC']))==-Decimal(r['amount']) and str(offset[0].get('AccountCode','')).strip()=='109419' and offset[0].get('Account')==DEBTOR and bank[0]['EntryID']==offset[0]['EntryID'])
            valid=valid and all(str(l.get('JournalCode','')).strip()=='27' and l['EntryNumber']==r['entry'] and l['Description']==r['description'] and l.get('Currency')=='EUR' and exact_date(l['Date'])==r['date'] for l in found) and offset[0].get('YourRef')==r['ref']
        (verified if valid else errors).append(r['payment_id'])
    occupied=sorted({l['EntryNumber'] for l in ledger}&{r['entry'] for r in manifest})
    for r in manifest:
        for l in ledger:
            if str(l.get('GLAccountCode','')).strip()!='1317':continue
            if re.search(r'\b'+re.escape(r['ref'])+r'\b',str(l.get('Description') or '')+' '+str(l.get('YourRef') or '')) and l not in hits[r['payment_id']]:possible.append(r['payment_id'])
    return {'verified_ids':verified,'existing_ids':[k for k,v in hits.items() if v],'errors':errors,'occupied_entries':occupied,'other_receipts_same_order':sorted(set(possible)),'complete':len(verified)==len(manifest) and not errors,'safe_to_import':not any(hits.values()) and not occupied and not possible}

class API:
    def __init__(self,app):
        from operations.allocation_connection import RoutingApp
        require(app.DIVISION==DIVISION and app.BASE_URL==BASE,'wrong_administration')
        self.app=RoutingApp(app);self.calls=0;self.quota={};self.last=0
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
    async def ledger(self):
        return await self.rows('financialtransaction/TransactionLines',{'$filter':f"FinancialYear eq 2026 and (JournalCode eq '27' or GLAccount eq guid'{BANK_GL}' or Account eq guid'{DEBTOR}')",'$select':SELECT,'$orderby':'EntryNumber,LineNumber'})

@fence.owned_operation('icepay')
async def run(mode):
    from app import main as app
    require(mode in {'prepare','apply','reconcile'},'invalid_mode')
    if mode=='apply':
        require(fence.current_owner() is not None and fence.current_owner().role=='icepay','assigned_worker_required')
        require(datetime.now(timezone.utc)<EXPIRES,'authorization_expired')
    task=JOB+':'+mode;data={};summary={'state':'started','financial_writes':False,'connection':'allocation'}
    conn=app._db_connect();locked=False;claimed=False
    def persist():
        conn.execute('UPDATE icepay_receipt_import_runs SET artifacts=%s::jsonb,summary=%s::jsonb WHERE task=%s',(json.dumps(data),json.dumps(summary),task))
    try:
        locked=conn.execute("SELECT pg_try_advisory_lock(hashtextextended('jnp:3977752:icepay:receipts',0))").fetchone()[0]
        require(locked,'another_icepay_writer')
        claimed=conn.execute('INSERT INTO icepay_receipt_import_runs(task) VALUES(%s) ON CONFLICT DO NOTHING RETURNING task',(task,)).fetchone() is not None
        require(claimed,'phase_already_claimed')
        source=conn.execute('SELECT status,artifacts,summary FROM icepay_transaction_tasks WHERE job=%s',(SOURCE,)).fetchone()
        parent=source[1]
        template=bytes(conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s',('FIBO-20260922-20261002',)).fetchone()[0])
        require(source[0]['state']=='downloaded','source_not_downloaded')
        require(source[1].get('refunds',{}).get('total')==0,'refunds_require_separate_processing')
        raw=base64.b64decode(source[1]['payments_csv'],validate=True);require(hashlib.sha256(raw).hexdigest()==source[2]['source_sha256'],'source_hash_changed')
        from operations.icepay_source_window import validate_source
        rows,source_summary,tz=validate_source(raw,parent['proof']['ui_payment_ids'],parent['table_evidence'],date(2026,10,6),date(2026,10,6))
        require(rows==source[1]['transactions'],'source_rows_changed')
        receipts=[r for r in rows if r['status']=='OK']
        require(len(receipts)==source[2]['td_ok_count'],'source_count_changed')
        require(sum((Decimal(r['amount']) for r in receipts),Decimal('0.00'))==Decimal(source[2]['td_ok_total']),'source_total_changed')
        xml,manifest=build_xml(receipts,template)
        sha=hashlib.sha256(xml).hexdigest();data.update(manifest=manifest,xml=base64.b64encode(xml).decode(),source_job=SOURCE,source_summary=source_summary)
        summary.update(receipts=len(receipts),total=source_summary['td_ok_total'],xml_sha256=sha,source_sha256=source[2]['source_sha256'],entries=[ENTRY])
        if mode!='prepare':
            prepared=conn.execute('SELECT artifacts,summary FROM icepay_receipt_import_runs WHERE task=%s',(JOB+':prepare',)).fetchone()
            require(prepared and prepared[1]['state'] in {'prepared','import_verified'} and prepared[0]['xml']==data['xml'] and prepared[0]['manifest']==manifest,'prepared_source_changed')
        api=API(app)
        js=await api.rows('financial/Journals',{'$filter':"Code eq '27'"})
        require(len(js)==1 and js[0]['ID']==JOURNAL and js[0]['GLAccount']==BANK_GL and js[0]['Currency']=='EUR' and js[0]['Type']==12 and js[0]['IsBlocked'] is False,'journal_changed')
        acc=await api.rows('crm/Accounts',{'$filter':"Code eq '            109419'",'$select':'ID,Code,IsSales,Status'})
        require(len(acc)==1 and acc[0]['ID']==DEBTOR and acc[0]['IsSales'] is True and acc[0]['Status']=='C','debtor_changed')
        before=await api.ledger();check=reconcile(manifest,before);data.update(before=before,before_check=check)
        summary.update(api_calls=api.calls,quota=api.quota,verified_receipts=len(check['verified_ids']))
        if check['complete']:summary.update(state='import_verified',already_present=True);return summary
        if mode=='reconcile':summary['state']='requires_review';return summary
        require(check['safe_to_import'],'existing_or_ambiguous_receipt')
        if mode=='prepare':summary['state']='prepared';return summary
        require(conn.execute('SELECT 1 FROM icepay_receipt_import_writes WHERE batch=%s',(JOB,)).fetchone() is None,'previous_write_reconcile_only')
        # One XML write, no generic retry helper. The Allocation integration
        # serializes token refresh/callback against the running routing service.
        token=await api.app._access_token()
        require(api.quota.get('daily',0)>200 and api.quota.get('minute',0)>8,'write_budget_low')
        write=conn.execute('INSERT INTO icepay_receipt_import_writes(batch,sha256) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING batch',(JOB,sha)).fetchone()
        require(bool(write),'previous_write_reconcile_only')
        summary.update(state='upload_requested',financial_writes=True,write_attempted=True,**fence.audit_metadata());persist()
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        try:
            async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
                response=await budgeted_http(api.app,'icepay','POST',lambda:client.post(BASE+'/docs/XMLUpload.aspx',params={'Topic':'GLTransactions','_Division_':str(DIVISION)},content=xml,headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'}),priority='critical',floor=200)
            parsed=ET.fromstring(response.content)
            data['xml_response']={'http_status':response.status_code,'body':ET.tostring(parsed,encoding='unicode') if parsed.tag in {'eExact','Messages','Message'} else 'unexpected_document'}
        except Exception as exc:
            summary.update(write_outcome='unknown_reconcile_required',write_error_type=type(exc).__name__)
        persist()
        after=await api.ledger();check=reconcile(manifest,after);data.update(after=after,after_check=check)
        summary.update(state='import_verified' if check['complete'] else 'requires_review_no_retry',verified_receipts=len(check['verified_ids']),api_calls=api.calls,quota=api.quota)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z0-9_]+',str(exc)) else type(exc).__name__
        summary.update(state='blocked',reason=reason)
    finally:
        if claimed:persist()
        if locked:conn.execute("SELECT pg_advisory_unlock(hashtextextended('jnp:3977752:icepay:receipts',0))")
        conn.close()
    if summary.get('write_attempted') and summary['state']!='import_verified':
        raise ValueError('icepay_upload_requires_review_no_retry')
    return summary

def main():
    import sys
    mode=sys.argv[1] if len(sys.argv)==2 else 'prepare'
    result=asyncio.run(run(mode));print(json.dumps(result or {'state':'phase_completed_read_durable_status'}))


def validate(job):
    require(job['task_key']==TASK and job['action']=='run' and job['params']=={},'invalid_oct06_command')
    require(datetime.now(timezone.utc)<EXPIRES,'authorization_expired')


async def run_task(app,job):
    validate(job)
    require(app.DIVISION==DIVISION and app.BASE_URL==BASE,'wrong_administration')
    for mode in ('prepare','apply'):
        result=await run(mode)
        require(result.get('state') in {'prepared','import_verified'},'oct06_'+mode+'_not_verified')
    from operations import icepay_automatic
    return await icepay_automatic.run(app,{**job,'task_key':icepay_automatic.TASK})

if __name__=='__main__':main()
