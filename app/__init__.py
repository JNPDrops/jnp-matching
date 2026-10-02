"""Opt-in GET-only 30-day bank reconciliation reference review.
Temporary audit output is kept in the private application database and logs.
No Exact business-data write is performed and no public route is installed.
"""
import os
if os.getenv('JNP_READONLY_AUDIT') == '20261002-100100-full-v4':
    import asyncio, json, re, time, hashlib
    from collections import Counter, defaultdict
    from datetime import datetime, timezone
    from decimal import Decimal
    from urllib.parse import urljoin, urlparse
    import httpx
    from . import main as m
    ACCOUNT = 'ec2af99c-809c-40e3-9057-8a31962ae1cf'
    START, END = '2026-09-03', '2026-10-03'
    BASE = 'https://start.exactonline.nl/api/v1/3977752/'
    FILTER_DATE = "Date ge datetime'2026-09-03T00:00:00' and Date lt datetime'2026-10-03T00:00:00'"
    def log(kind, data):
        print('JNP_AUDIT30_V4 '+kind+' '+json.dumps(data,ensure_ascii=True,default=str,separators=(',',':')),flush=True)
    def day(value):
        q=re.search(r'/Date\((-?\d+)', str(value))
        return datetime.fromtimestamp(int(q[1])/1000, timezone.utc).date().isoformat() if q else str(value)[:10]
    def amount(value):
        d=Decimal(str(value)); q=d.quantize(Decimal('0.01'))
        if not d.is_finite() or abs(d-q)>Decimal('0.000001'):
            raise ValueError('Invalid monetary value')
        return q
    def expected(text):
        if re.search(r'PAYNETICS|ICEPAY|MOLLIE|STRIPE|PAYPAL|ADYEN',text,re.I): return None,'PSP_OR_OTHER_METHOD'
        if 'JNPTEST' in text.upper(): return None,'TEST'
        nums=set(re.findall(r'\bTD\s*#?\s*(\d{4,10})\b',text,re.I))
        if len(nums)==1: return 'TD'+next(iter(nums)),'EXPLICIT_TD'
        if len(nums)>1: return None,'MULTIPLE_NUMBERS'
        nums=set(re.findall(r'(?<![A-Za-z0-9])(\d{4,10})(?![A-Za-z0-9])',text))
        if len(nums)==1: return 'TD'+next(iter(nums)),'NUMERIC_ORDER_CANDIDATE'
        return None,'MULTIPLE_NUMBERS' if nums else 'NO_ORDER'
    def save(key, data):
        with m._db_connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jnp_readonly_audit_cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at TIMESTAMPTZ NOT NULL DEFAULT NOW())')
            db.execute('INSERT INTO jnp_readonly_audit_cache(cache_key,payload) VALUES(%s,%s) ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,stored_at=NOW()', (key,json.dumps(data,default=str)))
    async def run():
        calls=0; stage='start'; began=datetime.now(timezone.utc).isoformat()
        try:
            if m.DIVISION !=3977752 or m.ENABLE_ORDER_RULE_WRITES or m.ENABLE_DIRECT_MATCH_WRITES: raise ValueError('Safety gate failed')
            async with httpx.AsyncClient(timeout=65) as c:
                async def read_all(path, params):
                    nonlocal calls
                    url=BASE+path; seen=set(); result=[]
                    for page in range(300):
                        if not url.startswith(BASE) or urlparse(url).netloc!='start.exactonline.nl': raise ValueError('Unsafe pagination URL')
                        for attempt in range(3):
                            if calls>=550: raise ValueError('Audit call budget reached')
                            await asyncio.sleep(1.15)
                            token=await m._access_token()
                            calls+=1
                            r=await c.get(url,params=params,headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
                            if r.status_code==429:
                                log('RATE_WAIT',{'calls':calls,'stage':stage})
                                await asyncio.sleep(65)
                                continue
                            if r.status_code>=400: raise ValueError('Exact GET '+path+' HTTP '+str(r.status_code)+' '+r.text[:180])
                            break
                        else: raise ValueError('Rate limit persists')
                        payload=r.json(); body=payload.get('d',payload)
                        if isinstance(body,list): rows=body; nxt=None
                        elif isinstance(body,dict) and isinstance(body.get('results'),list): rows=body['results']; nxt=body.get('__next')
                        else: raise ValueError('Invalid OData collection')
                        for row in rows: row.pop('__metadata',None)
                        result.extend(rows)
                        if not nxt: return result
                        url=urljoin(url,nxt); params=None
                        if url in seen: raise ValueError('Repeated cursor')
                        seen.add(url)
                        if page%20==19: log('PAGES',{'stage':stage,'rows':len(result),'calls':calls})
                    raise ValueError('Page limit reached')
                stage='banks'
                banks=await read_all('financialtransaction/BankEntryLines',{'$filter':"Account eq guid'"+ACCOUNT+"' and "+FILTER_DATE,'$select':'ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,GLAccountCode,OurRef,Modified','$orderby':'Date,ID'})
                if len({b['ID'] for b in banks})!=len(banks): raise ValueError('Duplicate bank IDs')
                for b in banks:
                    if str(b.get('Account')).lower()!=ACCOUNT or not START<=day(b.get('Date'))<END: raise ValueError('Bank filter mismatch')
                save('20261002-banks-100100-v4',banks)
                log('BANKS_COMPLETE',{'count':len(banks),'calls':calls,'first_date':min((day(b['Date']) for b in banks),default=None),'last_date':max((day(b['Date']) for b in banks),default=None)})
                entries=sorted({str(b['EntryID']) for b in banks})
                txs=[]; cfs=[]
                for offset in range(0,len(entries),8):
                    part=entries[offset:offset+8]
                    stage='ledger'
                    terms=' or '.join("EntryID eq guid'"+x+"'" for x in part)
                    rows=await read_all('financialtransaction/TransactionLines',{'$filter':"Account eq guid'"+ACCOUNT+"' and ("+terms+") and "+FILTER_DATE,'$select':'ID,EntryID,EntryNumber,LineNumber,Date,Description,AmountDC,AmountFC,Account,AccountCode,GLAccountCode,InvoiceNumber,YourRef,JournalCode,Currency,Status,Modified'})
                    txs.extend(rows)
                    log('LEDGER_PROGRESS',{'entries_completed':min(offset+8,len(entries)),'entries_total':len(entries),'ledger_rows':len(txs),'calls':calls})
                    stage='cashflow'
                    terms=' or '.join("TransactionEntryID eq guid'"+x+"'" for x in part)
                    rows=await read_all('cashflow/Receivables',{'$filter':"Account eq guid'"+ACCOUNT+"' and ("+terms+")",'$select':'ID,Account,AccountCode,TransactionID,TransactionEntryID,EntryNumber,InvoiceNumber,YourRef,IsFullyPaid,AmountDC,AmountFC,TransactionAmountDC,TransactionAmountFC,Currency,Source,Status,EndDate,EntryDate,Modified,Description'})
                    cfs.extend(rows)
                    log('CASHFLOW_PROGRESS',{'entries_completed':min(offset+8,len(entries)),'cashflow_rows':len(cfs),'calls':calls})
                save('20261002-ledger-100100-v4',txs); save('20261002-cashflow-100100-v4',cfs)
                by_tx={}; by_cf=defaultdict(list)
                for t in txs:
                    if t['ID'] in by_tx: raise ValueError('Duplicate ledger ID')
                    by_tx[t['ID']]=t
                for row in cfs: by_cf[row.get('TransactionID')].append(row)
                result=[]; counts=Counter(); amounts=defaultdict(lambda:Decimal(0)); exceptions=[]
                for b in banks:
                    t=by_tx.get(b['ID']); cf=by_cf.get(b['ID'],[])
                    ref,evidence=expected(str(b.get('Description') or ''))
                    actual=str((t or {}).get('YourRef') or '').strip()
                    cf_refs=sorted({str(x.get('YourRef') or '').strip() for x in cf if str(x.get('YourRef') or '').strip()})
                    cf_full=len(cf)==1 and cf[0].get('IsFullyPaid') is True and amount(cf[0].get('AmountDC',0))==0
                    amt=amount(b['AmountDC'])
                    valid_join=bool(t and t.get('EntryID')==b.get('EntryID') and t.get('LineNumber')==b.get('LineNumber') and amount(t['AmountDC'])==-amt)
                    if not valid_join: verdict='IDENTITY_REVIEW'
                    elif evidence=='TEST': verdict='TEST'
                    elif amt<=0: verdict='REFUND_OR_DEBIT'
                    elif evidence=='PSP_OR_OTHER_METHOD': verdict='PSP_OR_OTHER_METHOD'
                    elif not ref: verdict='NO_UNIQUE_ORDER'
                    elif not actual: verdict='NO_SELECTED_REFERENCE'
                    elif actual.upper()==ref.upper(): verdict='REFERENCE_EQUAL'
                    else: verdict='REFERENCE_DIFFERENCE'
                    if cf_refs and actual and actual not in cf_refs: verdict='SOURCE_CONFLICT'
                    counts[verdict]+=1; amounts[verdict]+=amt
                    out={'bank_id':b['ID'],'entry_id':b['EntryID'],'entry_number':b['EntryNumber'],'line_number':b['LineNumber'],'date':day(b['Date']),'description':b.get('Description'),'amount_eur':str(amt),'expected_ref':ref,'selected_ref':actual or None,'selected_invoice_number':(t or {}).get('InvoiceNumber'),'bank_our_ref':b.get('OurRef'),'order_evidence':evidence,'result':verdict,'cashflow_records':len(cf),'cashflow_refs':cf_refs,'cashflow_fully_paid':cf_full,'journal':(t or {}).get('JournalCode'),'currency':(t or {}).get('Currency'),'identity_verified':valid_join}
                    result.append(out)
                    if verdict in ('REFERENCE_DIFFERENCE','SOURCE_CONFLICT','IDENTITY_REVIEW'): exceptions.append(out)
                stage='invoice_verification'
                invoice_nums=sorted({int(x['selected_invoice_number']) for x in exceptions if x.get('selected_invoice_number')})
                invoices=[]
                for off in range(0,len(invoice_nums),20):
                    terms=' or '.join('EntryNumber eq '+str(n) for n in invoice_nums[off:off+20])
                    rows=await read_all('financialtransaction/TransactionLines',{'$filter':"Account eq guid'"+ACCOUNT+"' and ("+terms+")",'$select':'ID,EntryID,EntryNumber,LineNumber,Account,AccountCode,GLAccountCode,AmountDC,AmountFC,YourRef,JournalCode,Currency'})
                    invoices.extend(rows)
                save('20261002-audit-100100-v4',{'rows':result,'invoice_evidence':invoices,'started_at':began,'finished_at':datetime.now(timezone.utc).isoformat()})
                for i,row in enumerate(exceptions,1): log('EXCEPTION',{'n':i,**row})
                for i in range(0,len(invoices),10): log('INVOICE_EVIDENCE',invoices[i:i+10])
                for category in ('REFERENCE_EQUAL','NO_UNIQUE_ORDER','NO_SELECTED_REFERENCE','REFUND_OR_DEBIT','PSP_OR_OTHER_METHOD','TEST'):
                    log('SAMPLE_'+category,[r for r in result if r['result']==category][:5])
                log('COMPLETE',{'from':START,'to_inclusive':'2026-10-02','date_basis':'bank booking date','started_at':began,'finished_at':datetime.now(timezone.utc).isoformat(),'bank_rows':len(banks),'ledger_rows':len(txs),'cashflow_rows':len(cfs),'counts':dict(counts),'amounts_eur':{k:str(v) for k,v in amounts.items()},'exceptions':len(exceptions),'bank_entries':len(entries),'read_calls':calls,'all_pages_read':True,'bank_rows_with_cashflow':sum(bool(r['cashflow_records']) for r in result),'cashflow_fully_paid_rows':sum(r['cashflow_fully_paid'] for r in result),'exact_financial_writes':0,'audit_cache_key':'20261002-audit-100100-v4','note':'Reference review; bank rows without cashflow linkage cannot be declared fully verified reconciliation.'})
        except Exception as exc:
            log('ERROR',{'stage':stage,'calls':calls,'type':type(exc).__name__,'detail':str(exc)[:400],'complete':False,'exact_financial_writes':0})
    @m.app.on_event('startup')
    async def start_audit():
        async def delayed():
            await asyncio.sleep(8)
            await asyncio.wait_for(run(),timeout=1200)
        m.app.state.audit30_task=asyncio.create_task(delayed())
