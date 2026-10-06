"""One new Fibonatix receipt batch through 5 Oct Amsterdam, never a retry.

Private durable sources and old template remain unchanged. Uses the existing
Allocation connection's refresh lock and Fibonatix's global PostgreSQL lock.
No payouts/fees/refunds are inferred. No historical financial repair.
"""
import asyncio,base64,copy,hashlib,json,re
from decimal import Decimal
import xml.etree.ElementTree as ET
from operations.icepay_window_import import API,SELECT,BASE,DIVISION,TEMPLATE_SHA,require,exact_date
from operations.fibonatix_window_source import parse,timezone_offset
JOB='FIBO-20261003-05-AMSTERDAM'
LOCK=397775226
DEBTOR='ec2af99c-809c-40e3-9057-8a31962ae1cf'
JOURNAL='60f578ce-eb38-43c0-955a-5ab464f84ce3'
BANK_GL='8edb713c-cc61-4f75-a519-67a7a79d2b0e'
SOURCE_SHA='2e9053ffc622452f0732538049decbb8a12c0885828a48a463ef6282f1572eb2'
DAYS={'2026-10-03':(66,Decimal('6754.44'),26260020),'2026-10-04':(80,Decimal('8351.67'),26260021),'2026-10-05':(102,Decimal('11773.63'),26260022)}

def source(conn):
    def read(key):return conn.execute('SELECT result FROM paragon_login_probes WHERE probe_id=%s',(key,)).fetchone()[0]
    proof=read('fibonatix-validated-source-20261005-v1')
    raw=base64.b64decode(read('paragon-existing-export-20261003-05-v1')['source_csv'],validate=True)
    require(hashlib.sha256(raw).hexdigest()==SOURCE_SHA,'source_hash_changed')
    require(proof['cutoff_exclusive']=='2026-10-06T00:00:00+02:00','cutoff_changed')
    offset=timezone_offset(raw,proof['timezone_evidence']);require(offset==2,'timezone_changed')
    rows,summary=parse(raw,read('fibonatix-order-evidence-20261003-05-v1')['orders'],offset_hours=offset,allowed_dates=set(DAYS))
    require(rows==proof['rows'] and summary==proof['summary'],'validated_source_changed')
    return rows,summary

def build_xml(rows,template):
    require(hashlib.sha256(template).hexdigest()==TEMPLATE_SHA,'template_changed')
    require(len(rows)==248 and len({r['trx'] for r in rows})==248 and len({r['ref'] for r in rows})==248,'source_count_changed')
    for r in rows:require(re.fullmatch(r'[A-Za-z0-9]{6,32}',r['trx']) and re.fullmatch(r'TD\d+',r['ref']) and type(r['woo_id']) is int and r['date'] in DAYS and Decimal(r['amount'])>0,'invalid_source')
    old=ET.fromstring(template)
    et,lt=next((e,l) for e in old.findall('./GLTransactions/GLTransaction') for l in e.findall('GLTransactionLine') if l.find('GLAccount').get('code')=='1100' and l.find('Account') is not None and l.find('Account').get('code')=='100100' and Decimal(l.findtext('Amount/Value'))>0)
    require([c.tag for c in et if c.tag!='GLTransactionLine']==['TransactionType','Journal','Date','FinYear','FinPeriod','Description'],'header_schema_changed')
    require([c.tag for c in lt]==['Date','VATType','FinYear','FinPeriod','GLAccount','Description','Account','Amount','References','Note'],'line_schema_changed')
    root=ET.Element('eExact');txs=ET.SubElement(root,'GLTransactions');manifest=[]
    for day,(count,total,entry) in DAYS.items():
        selected=sorted((r for r in rows if r['date']==day),key=lambda r:r['trx'])
        require(len(selected)==count and sum((Decimal(r['amount']) for r in selected),Decimal(0))==total,'day_total_changed')
        e=ET.SubElement(txs,'GLTransaction',entry=str(entry))
        for child in et:
            if child.tag!='GLTransactionLine':e.append(copy.deepcopy(child))
        e.find('Journal').set('code','26');e.find('Date').text=day;e.find('FinYear').set('number','2026');e.find('FinPeriod').set('number','10');e.find('Description').text=JOB+' '+day
        for n,r in enumerate(selected,1):
            l=copy.deepcopy(lt);l.set('line',str(n));l.find('Date').text=day;l.find('FinYear').set('number','2026');l.find('FinPeriod').set('number','10')
            desc='Fibonatix '+r['ref']+' | Woo '+str(r['woo_id'])+' | Betaling '+r['trx']
            l.find('GLAccount').set('code','1100');l.find('Account').set('code','100100');l.find('Description').text=desc
            l.find('Amount/Currency').set('code','EUR');l.find('Amount/Value').text=r['amount'];l.find('References/PaymentReference').text=r['trx'];l.find('References/YourRef').text=r['ref'];l.find('Note').text=JOB+' | '+desc;e.append(l)
            manifest.append({**r,'entry':entry,'description':desc})
    return ET.tostring(root,encoding='utf-8',xml_declaration=True),manifest

def reconcile(manifest,ledger):
    hits={r['trx']:[] for r in manifest};verified=[];errors=[];possible=[]
    for line in ledger:
        tokens=set(re.findall(r'\b[A-Za-z0-9]+\b',str(line.get('Description') or '')+' '+str(line.get('PaymentReference') or '')))
        for key in hits.keys()&tokens:hits[key].append(line)
    for r in manifest:
        found=hits[r['trx']]
        if not found:continue
        bank=[l for l in found if str(l.get('GLAccountCode','')).strip()=='1316'];offset=[l for l in found if str(l.get('GLAccountCode','')).strip()=='1100']
        valid=len(found)==2 and len(bank)==1 and len(offset)==1
        if valid:
            valid=Decimal(str(bank[0]['AmountDC']))==Decimal(r['amount']) and Decimal(str(offset[0]['AmountDC']))==-Decimal(r['amount']) and offset[0].get('Account')==DEBTOR and str(offset[0].get('AccountCode','')).strip()=='100100' and bank[0]['EntryID']==offset[0]['EntryID']
            valid=valid and all(str(l.get('JournalCode','')).strip()=='26' and l['EntryNumber']==r['entry'] and l['Description']==r['description'] and l.get('Currency')=='EUR' and exact_date(l['Date'])==r['date'] for l in found) and offset[0].get('YourRef')==r['ref']
        (verified if valid else errors).append(r['trx'])
    occupied=sorted({l['EntryNumber'] for l in ledger}&{r['entry'] for r in manifest})
    for r in manifest:
        for l in ledger:
            if str(l.get('GLAccountCode','')).strip()=='1316' and re.search(r'\b'+r['ref']+r'\b',str(l.get('Description') or '')+' '+str(l.get('YourRef') or '')) and l not in hits[r['trx']]:possible.append(r['trx'])
    return {'verified_ids':verified,'existing_ids':[k for k,v in hits.items() if v],'errors':errors,'occupied_entries':occupied,'other_receipts_same_order':sorted(set(possible)),'complete':len(verified)==len(manifest) and not errors,'safe_to_import':not any(hits.values()) and not occupied and not possible}

async def ledger(api,manifest,*,before):
    rows=await api.rows('financialtransaction/TransactionLines',{'$filter':"JournalCode eq '26' and Date ge datetime'2026-10-03T00:00:00'",'$select':SELECT})
    if before:
        # Cross-period and cross-journal PSP-ID duplicate check, bounded chunks.
        for i in range(0,len(manifest),12):
            query=' or '.join("PaymentReference eq '"+r['trx']+"'" for r in manifest[i:i+12])
            rows+=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':SELECT})
    return list({r['ID']:r for r in rows}.values())

async def run(mode):
    from app import main
    require(mode in {'prepare','apply','reconcile'},'invalid_mode')
    conn=main._db_connect();locked=claimed=False;data={};summary={'state':'started','financial_writes':False,'connection':'allocation'}
    def persist():conn.execute('INSERT INTO fibonatix_import_artifacts(job,name,data) VALUES(%s,%s,%s::jsonb) ON CONFLICT(job,name) DO UPDATE SET data=EXCLUDED.data',(JOB,mode,json.dumps({'summary':summary,'artifacts':data})))
    try:
        locked=conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0];require(locked,'another_fibonatix_writer')
        claimed=conn.execute('INSERT INTO fibonatix_import_attempts(job,action) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING action',(JOB,mode)).fetchone() is not None;require(claimed,'phase_already_claimed')
        rows,ss=source(conn);template=bytes(conn.execute('SELECT xml FROM fibonatix_import_jobs WHERE job=%s',('FIBO-20260922-20261002',)).fetchone()[0])
        xml,manifest=build_xml(rows,template);sha=hashlib.sha256(xml).hexdigest();data.update(manifest=manifest,source_summary=ss,xml_sha256=sha)
        summary.update(receipts=248,total='26879.74',entries=[v[2] for v in DAYS.values()],xml_sha256=sha)
        if mode!='prepare':
            p=conn.execute('SELECT sha,xml,data FROM fibonatix_import_jobs WHERE job=%s',(JOB,)).fetchone();require(p and p[0]==sha and bytes(p[1])==xml and p[2]['manifest']==manifest,'prepared_source_changed')
        api=API(main)
        js=await api.rows('financial/Journals',{'$filter':"Code eq '26'"})
        require(len(js)==1 and js[0]['ID']==JOURNAL and js[0]['GLAccount']==BANK_GL and js[0]['Currency']=='EUR' and js[0]['Type']==12 and js[0]['IsBlocked'] is False,'journal_changed')
        acc=await api.rows('crm/Accounts',{'$filter':"Code eq '            100100'",'$select':'ID,Code,IsSales,Status'})
        require(len(acc)==1 and acc[0]['ID']==DEBTOR and acc[0]['IsSales'] is True and acc[0]['Status']=='C','debtor_changed')
        before=await ledger(api,manifest,before=mode!='reconcile');check=reconcile(manifest,before);data.update(before=before,before_check=check);summary.update(api_calls=api.calls,quota=api.quota,verified_receipts=len(check['verified_ids']))
        if check['complete']:summary.update(state='import_verified',already_present=True);return summary
        if mode=='reconcile':summary['state']='requires_review';return summary
        require(check['safe_to_import'],'existing_or_ambiguous_receipt')
        if mode=='prepare':
            require(conn.execute('INSERT INTO fibonatix_import_jobs(job,sha,xml,data) VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING job',(JOB,sha,xml,json.dumps({'manifest':manifest,'summary':ss,'status':'prepared'}))).fetchone(),'job_already_exists')
            summary['state']='prepared';return summary
        token=await api.app._access_token();require(api.quota.get('daily',0)>200 and api.quota.get('minute',0)>8,'write_budget_low')
        require(conn.execute('INSERT INTO fibonatix_import_attempts(job,action) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING action',(JOB,'xml_upload')).fetchone(),'previous_write_reconcile_only')
        summary.update(state='upload_requested',financial_writes=True);persist()
        import httpx
        from operations.bacs_debtor_transfer import TLS_CONTEXT
        try:
            async with httpx.AsyncClient(timeout=180,follow_redirects=False,trust_env=False,verify=TLS_CONTEXT) as client:
                response=await client.post(BASE+'/docs/XMLUpload.aspx',params={'Topic':'GLTransactions','_Division_':str(DIVISION)},content=xml,headers={'Authorization':'Bearer '+token,'Content-Type':'application/xml; charset=utf-8','Accept':'application/xml,text/xml'})
            parsed=ET.fromstring(response.content);data['xml_response']={'http_status':response.status_code,'body':ET.tostring(parsed,encoding='unicode') if parsed.tag in {'eExact','Messages','Message'} else 'unexpected_document'}
        except Exception as exc:summary.update(write_outcome='unknown_reconcile_required',write_error_type=type(exc).__name__)
        persist();after=await ledger(api,manifest,before=False);check=reconcile(manifest,after);data.update(after=after,after_check=check)
        summary.update(state='import_verified' if check['complete'] else 'requires_review_no_retry',verified_receipts=len(check['verified_ids']),api_calls=api.calls,quota=api.quota)
    except Exception as exc:
        summary.update(state='blocked',reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z0-9_]+',str(exc)) else type(exc).__name__)
    finally:
        if claimed:persist()
        if locked:conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
        conn.close()
    return summary
if __name__=='__main__':
    import sys
    print(json.dumps(asyncio.run(run(sys.argv[1]))))
