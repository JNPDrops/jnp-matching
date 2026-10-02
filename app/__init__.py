"""GET-only enrichment of an existing private reconciliation audit snapshot."""
import os
if os.getenv('JNP_READONLY_AUDIT') == '20261002-100100-refine-v5':
    import asyncio,json,re,unicodedata
    from datetime import datetime,timezone
    from decimal import Decimal
    from collections import Counter,defaultdict
    from urllib.parse import urljoin
    import httpx
    from . import main as m
    A='ec2af99c-809c-40e3-9057-8a31962ae1cf'
    BASE='https://start.exactonline.nl/api/v1/3977752/'
    def log(kind,data):
        print('JNP_AUDIT30_V5 '+kind+' '+json.dumps(data,ensure_ascii=True,default=str,separators=(',',':')),flush=True)
    def expected(text):
        text=unicodedata.normalize('NFKC',str(text))
        if re.search(r'\b(?:PAYNETICS|ICEPAY|MOLLIE|STRIPE|PAYPAL|ADYEN)\b',text,re.I):return None,'PSP_OR_OTHER_METHOD'
        if 'JNPTEST' in text.upper():return None,'TEST'
        text=re.sub(r'(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}(?!\d)',' ',text)
        text=re.sub(r'(?<!\d)(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}(?!\d)',' ',text)
        def compact_date(match):
            for fmt in ('%d%m%Y','%Y%m%d'):
                try:
                    d=datetime.strptime(match[0],fmt)
                    if 1990<=d.year<=2100:return ' '
                except ValueError:pass
            return match[0]
        text=re.sub(r'\b\d{8}\b',compact_date,text)
        nums=set(re.findall(r'\bTD\s*#?\s*(\d{4,10})\b',text,re.I))
        if len(nums)==1:return 'TD'+next(iter(nums)),'EXPLICIT_TD'
        if len(nums)>1:return None,'MULTIPLE_ORDERS'
        nums=set(re.findall(r'\b(?:order(?:nummer)?|commande|ordine|bestelling)\s*(?:n[o\u00b0\u00ba]?\.?|nr\.?|number|nummer|#)?\s*(\d{4,10})\b',text,re.I))
        nums.update(re.findall(r'\b(?:n[o\u00b0\u00ba]?\.?|nr\.?)\s*#?\s*(\d{4,10})\b',text,re.I))
        if len(nums)==1:return 'TD'+next(iter(nums)),'EXPLICIT_ORDER_CONTEXT'
        if len(nums)>1:return None,'MULTIPLE_ORDERS'
        nums={n for n in re.findall(r'(?<![A-Za-z0-9])(\d{4,10})(?![A-Za-z0-9])',text) if not (len(n)==4 and 1900<=int(n)<=2100)}
        if len(nums)==1:return 'TD'+next(iter(nums)),'NUMERIC_ORDER_CANDIDATE'
        return None,'MULTIPLE_NUMBERS' if nums else 'NO_ORDER'
    async def run():
        try:
            if m.DIVISION!=3977752 or m.ENABLE_ORDER_RULE_WRITES or m.ENABLE_DIRECT_MATCH_WRITES:raise ValueError('Safety gate failed')
            with m._db_connect() as db:
                saved=db.execute('SELECT payload FROM jnp_readonly_audit_cache WHERE cache_key=%s',('20261002-audit-100100-v4',)).fetchone()
            if not saved:raise ValueError('Audit snapshot missing')
            audit=json.loads(saved[0]); rows=audit['rows']; counts=Counter(); sums=defaultdict(lambda:Decimal(0)); changed=[]
            for r in rows:
                previous=r['result']; ref,evidence=expected(r['description'] or '')
                r['expected_ref']=ref;r['order_evidence']=evidence
                if not r['identity_verified']:result='IDENTITY_REVIEW'
                elif evidence=='TEST':result='TEST'
                elif Decimal(r['amount_eur'])<=0:result='REFUND_OR_DEBIT'
                elif evidence=='PSP_OR_OTHER_METHOD':result='PSP_OR_OTHER_METHOD'
                elif not ref:result='NO_UNIQUE_ORDER'
                elif not r['selected_ref']:result='NO_SELECTED_REFERENCE'
                elif ref.upper()==r['selected_ref'].upper():result='REFERENCE_EQUAL'
                else:result='REFERENCE_DIFFERENCE'
                r['result']=result;counts[result]+=1;sums[result]+=Decimal(r['amount_eur'])
                if previous!=result:changed.append({'bank_id':r['bank_id'],'description':r['description'],'old_result':previous,'new_result':result,'expected_ref':ref,'selected_ref':r['selected_ref']})
            flags=[r for r in rows if r['result']=='REFERENCE_DIFFERENCE']
            refs=sorted({r['expected_ref'] for r in flags}|{r['selected_ref'] for r in flags})
            invoices=[]
            if refs:
                filt="Account eq guid'"+A+"' and GLAccountCode eq '1100' and JournalCode eq '70' and ("+' or '.join("YourRef eq '"+r+"'" for r in refs)+')'
                url=BASE+'financialtransaction/TransactionLines';params={'$filter':filt,'$select':'ID,EntryID,EntryNumber,Date,LineNumber,AmountDC,AmountFC,Account,AccountCode,YourRef,JournalCode,Currency,GLAccountCode,Description'}
                async with httpx.AsyncClient(timeout=60) as c:
                    for page in range(40):
                        if not url.startswith(BASE):raise ValueError('Unexpected cursor')
                        await asyncio.sleep(1.5)
                        token=await m._access_token()
                        q=await c.get(url,params=params,headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
                        if q.status_code>=400:raise ValueError('Invoice read HTTP '+str(q.status_code))
                        body=q.json().get('d',{})
                        invoices.extend(body.get('results',[]))
                        if not body.get('__next'):break
                        url=urljoin(url,body['__next']);params=None
                    else:raise ValueError('Invoice paging limit')
            for r in invoices:r.pop('__metadata',None)
            for r in flags:
                r['expected_invoice_candidates']=[i for i in invoices if i.get('YourRef')==r['expected_ref']]
                r['selected_invoice_candidates']=[i for i in invoices if i.get('YourRef')==r['selected_ref'] and i.get('EntryNumber')==r['selected_invoice_number']]
                r['assessment']='ORDER_REFERENCE_DIFFERENCE_REQUIRES_REVIEW'
                for key in ('expected_invoice_candidates','selected_invoice_candidates'):
                    if len(r[key])==1:r[key][0]['bank_amount_equals_invoice_amount']=Decimal(str(r[key][0]['AmountDC'])).quantize(Decimal('0.01'))==Decimal(r['amount_eur'])
                log('FLAG',r)
            log('PARSER_CORRECTIONS',changed)
            details=defaultdict(lambda:{'rows':0,'amount':Decimal(0)})
            for r in rows:
                if r['result']=='PSP_OR_OTHER_METHOD':
                    match=re.search(r'\b(PAYNETICS|ICEPAY|MOLLIE|STRIPE|PAYPAL|ADYEN)\b',r['description'],re.I)
                    provider=match[1].upper() if match else 'OTHER'
                    details[provider]['rows']+=1;details[provider]['amount']+=Decimal(r['amount_eur'])
            summary={'period_from':'2026-09-03','period_to':'2026-10-02','date_basis':'bank booking date','snapshot_started_at':audit['started_at'],'snapshot_finished_at':audit['finished_at'],'refined_at':datetime.now(timezone.utc).isoformat(),'bank_booking_rows':len(rows),'counts':dict(counts),'amounts_eur':{k:str(v) for k,v in sums.items()},'net_amount_eur':str(sum(sums.values())),'provider_breakdown':{k:{'rows':v['rows'],'amount_eur':str(v['amount'])} for k,v in details.items()},'currencies':dict(Counter(r['currency'] for r in rows)),'changed_classifications':len(changed),'flag_count':len(flags),'cashflow_linked_bank_rows':sum(bool(r['cashflow_records']) for r in rows),'all_bank_and_ledger_pages_read':True,'exact_financial_writes':0,'qualification':'Reference-consistency review; complete payment-to-invoice match graph not verified.'}
            with m._db_connect() as db:
                db.execute('INSERT INTO jnp_readonly_audit_cache(cache_key,payload) VALUES(%s,%s) ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,stored_at=NOW()',('20261002-audit-100100-final',json.dumps({'summary':summary,'rows':rows,'flags':flags,'parser_corrections':changed},default=str)))
            log('COMPLETE',summary)
        except Exception as exc:log('ERROR',{'type':type(exc).__name__,'detail':str(exc)[:300],'exact_financial_writes':0})
    @m.app.on_event('startup')
    async def start_refinement():
        async def delayed():
            await asyncio.sleep(8);await run()
        m.app.state.audit_refinement_task=asyncio.create_task(delayed())
