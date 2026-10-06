"""Own-order Fibonatix matching for the verified new October receipt batch.

At most five saves per claimed group. An uncertain save blocks later groups.
No historical repair, write-off, inferred fees or cross-order settlement.
"""
import asyncio,json,re
from collections import Counter
from decimal import Decimal
from operations.fibonatix_window_import import JOB,LOCK,DEBTOR,API,SELECT,reconcile,require

def money(v):
    d=Decimal(str(v));require(d.is_finite() and d==d.quantize(Decimal('.01')),'invalid_money');return d

def code(row,key):return str(row.get(key) or '').strip()
def now():
    from datetime import datetime,timezone
    return datetime.now(timezone.utc).isoformat()

def open_rows(r,rows):
    return ([x for x in rows if code(x,'JournalCode')=='70' and x.get('YourRef')==r['ref'] and str(x.get('InvoiceNumber'))==str(r['invoice']['EntryNumber'])],
            [x for x in rows if code(x,'JournalCode')=='26' and r['trx'] in str(x.get('Description') or '')])

def validate_open(r,opened):
    invoices,credits=open_rows(r,opened)
    if len(invoices)!=1 or len(credits)!=1:return 'invoice_closed_or_receipt_not_fully_open'
    for row,amount in [(invoices[0],money(r['amount'])),(credits[0],-money(r['amount']))]:
        if code(row,'AccountId')!=DEBTOR or code(row,'AccountCode')!='100100' or row.get('CurrencyCode')!='EUR':return 'open_item_identity_changed'
        if money(row['Amount'])!=amount or money(row.get('AmountInTransit') or 0)!=0:return 'partial_amount_or_in_transit'
    if credits[0].get('YourRef')!=r['ref'] or credits[0].get('Description')!=r['description']:return 'receipt_reference_changed'

def classify(r,history,opened):
    candidates=[x for x in history if x.get('YourRef')==r['ref'] and code(x,'JournalCode')=='70' and code(x,'GLAccountCode')=='1100' and money(x['AmountDC'])>0]
    r['invoice_candidates']=candidates
    if not candidates:return 'invoice_missing_webshop_'+str(r['order_status'])
    if len(candidates)!=1:return 'multiple_invoices_for_order'
    r['invoice']=invoice=candidates[0];r['difference']=str(money(r['amount'])-money(invoice['AmountDC']))
    if invoice.get('Account')!=DEBTOR or code(invoice,'AccountCode')!='100100':return 'invoice_on_other_debtor'
    if invoice.get('Currency')!='EUR':return 'invoice_currency_mismatch'
    if money(invoice['AmountDC'])!=money(r['amount']):return 'amount_difference_requires_case_decision'
    if r['order_status']=='cancelled':return 'cancelled_order_requires_review'
    return validate_open(r,opened)

def set_exception(r,reason):
    r.update(exception=reason,state='exception',workflow_status='open',execution_status='not_executed',suggested_action='Review this own order/invoice/payment; processing is not a promise of future import. No cross-order settlement or automatic write-off.')

def stats(plan):
    rs=plan['receipts'];return {'receipts':len(rs),'matched':sum(r['state']=='matched_verified' for r in rs),'matched_total':str(sum((money(r['amount']) for r in rs if r['state']=='matched_verified'),Decimal('0.00'))),'pending':sum(r['state']=='pending' for r in rs),'unknown_saves':sum(r['state']=='match_requested' for r in rs),'exception_counts':dict(Counter(r['exception'] for r in rs if r['exception']))}

def approved(plan,imported):
    require(imported['summary']['state']=='import_verified' and imported['summary']['verified_receipts']==248,'verified_import_required')
    require(plan['manifest']==imported['artifacts']['manifest'] and len(plan['receipts'])==248,'plan_source_changed')
    require(len({r['trx'] for r in plan['receipts']})==248 and len({r['bank_line_id'] for r in plan['receipts']})==248,'duplicate_identity')
    by={r['trx']:r for r in plan['manifest']}
    for r in plan['receipts']:require(r['trx'] in by and all(r[k]==by[r['trx']][k] for k in ['amount','ref','description','entry','woo_id']) and r['source_order']==r['ref'],'receipt_source_changed')
    require(not any(r['state']=='match_requested' for r in plan['receipts']),'unknown_save_requires_readback')

def save_plan(conn,plan):
    from operations.fibonatix_import import encode_evidence
    conn.execute('INSERT INTO fibonatix_import_artifacts(job,name,data) VALUES(%s,%s,%s::jsonb) ON CONFLICT(job,name) DO UPDATE SET data=EXCLUDED.data',(JOB,'window_matching_plan',json.dumps(encode_evidence(plan))))

def load_plan(conn):
    from operations.fibonatix_import import decode_evidence
    return decode_evidence(conn.execute('SELECT data FROM fibonatix_import_artifacts WHERE job=%s AND name=%s',(JOB,'window_matching_plan')).fetchone()[0])

def claim_save(conn,task,r,plan):
    with conn.transaction():
        require(conn.execute('INSERT INTO fibonatix_import_attempts(job,action) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING action',(JOB,'match:'+r['bank_line_id'])).fetchone(),'previous_save_reconcile_only')
        r.update(state='match_requested',execution_status='unknown_readback_required');r['attempts'].append({'at':now(),'task':task,'outcome':'unknown_readback_required'});save_plan(conn,plan)

def verify_group_readback(r,lines,opened):
    require(reconcile([r],lines)['complete'],'source_changed_after_save')
    invoices,credits=open_rows(r,opened);require(not invoices and not credits,'saved_match_not_closed')

async def search_own_invoice(frame,receipt):
    # Exact's default grid shows only 99 entries. Use its observed Search
    # control, preserving the receipt/account context and all save checks.
    from operations.strict_order_matching import euro
    await frame.locator('#Search').fill(receipt['ref'])
    await frame.locator('#btnSearch').click()
    await frame.locator('tr[id^=List_row_]').filter(has_text=receipt['ref']).first.wait_for(state='visible',timeout=20000)
    require(await frame.locator('#Search').input_value()==receipt['ref'],'search_reference_changed')
    require(await frame.locator('#Account_alt').input_value()=='100100' and await frame.locator('#GLAccount_alt').input_value()=='1100' and euro(await frame.locator('#EntryAmount').input_value())==money(receipt['amount']),'search_context_changed')

async def run(mode):
    from app import main
    from operations import icepay_matching as reader
    from operations.strict_order_matching import session,open_match,match_rows,toggle,euro
    require(mode=='prepare' or (isinstance(mode,int) and 1<=mode<=60),'invalid_mode')
    task='match_prepare' if mode=='prepare' else 'match_group_'+str(mode).zfill(2)
    conn=main._db_connect();locked=claimed=False;plan={};summary={'state':'started','financial_saves_this_run':0,'task':task};api=API(main);calls=0
    def persist():
        if plan:save_plan(conn,plan);summary.update(stats(plan))
        summary.update(api_calls=calls+api.calls,quota=api.quota)
        conn.execute('INSERT INTO fibonatix_import_artifacts(job,name,data) VALUES(%s,%s,%s::jsonb) ON CONFLICT(job,name) DO UPDATE SET data=EXCLUDED.data',(JOB,task,json.dumps(summary)))
    async def orders(refs,history=False):return await reader.Reader.orders(api,refs,history=history)
    async def lines(r):return await api.rows('financialtransaction/TransactionLines',{'$filter':"EntryID eq guid'"+r['entry_id']+"' and (ID eq guid'"+r['bank_line_id']+"' or ID eq guid'"+r['offset_id']+"')",'$select':SELECT})
    try:
        locked=conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0];require(locked,'another_fibonatix_writer')
        claimed=conn.execute('INSERT INTO fibonatix_import_attempts(job,action) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING action',(JOB,task)).fetchone() is not None;require(claimed,'phase_already_claimed')
        imported=conn.execute('SELECT data FROM fibonatix_import_artifacts WHERE job=%s AND name=%s',(JOB,'apply')).fetchone()[0]
        if mode=='prepare':
            require(imported['summary']['state']=='import_verified' and imported['summary']['verified_receipts']==248,'verified_import_required')
            manifest=imported['artifacts']['manifest'];ledger=imported['artifacts']['after'];require(reconcile(manifest,ledger)['complete'],'source_ledger_changed')
            receipts=[]
            for r in manifest:
                found=[l for l in ledger if l['Description']==r['description']];bank=next(l for l in found if code(l,'GLAccountCode')=='1316');offset=next(l for l in found if code(l,'GLAccountCode')=='1100')
                receipts.append({**r,'source_order':r['ref'],'bank_line_id':bank['ID'],'offset_id':offset['ID'],'entry_id':bank['EntryID'],'state':'pending','invoice':None,'exception':None,'evidence':[],'attempts':[],'division':3977752,'psp':'Fibonatix','journal':'26','debtor':'100100','currency':'EUR','workflow_status':'open','execution_status':'not_executed'})
            plan={'manifest':manifest,'receipts':receipts,'prepared_at':now(),'cutoff_exclusive':'2026-10-06T00:00:00+02:00'}
            for i in range(0,len(receipts),48):
                chunk=receipts[i:i+48];refs=[r['ref'] for r in chunk];history=await orders(refs,True);opened=await orders(refs)
                for r in chunk:
                    reason=classify(r,history,opened)
                    if reason:set_exception(r,reason)
                persist();calls+=api.calls;api=API(main)
            summary['state']='prepared';return summary
        plan=load_plan(conn);approved(plan,imported)
        queue=[r for r in plan['receipts'] if r['state']=='pending'][:5]
        if not queue:summary['state']='no_pending_items';return summary
        refs=[r['ref'] for r in queue]
        history=await orders(refs,True);initial_open=await orders(refs)
        ids=[r[k] for r in queue for k in ['bank_line_id','offset_id']]
        query=' or '.join("ID eq guid'"+v+"'" for v in ids)
        initial_lines=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':SELECT})
        saved=[]
        async with session() as (context,page):
            for r in queue:
                summary['state']='processing';persist()
                opened=initial_open;reason=classify(r,history,opened)
                if reason:set_exception(r,reason);persist();continue
                require(reconcile([r],[x for x in initial_lines if x['ID'] in {r['bank_line_id'],r['offset_id']}])['complete'],'source_entry_changed')
                frame=await open_match(context,page,r);await search_own_invoice(frame,r);ui=await match_rows(frame);r['evidence'].append({'at':now(),'phase':'before','rows':ui,'open_items':opened})
                if any(x['checked'] for x in ui):set_exception(r,'existing_match_requires_inspection');persist();continue
                hits=[x for x in ui if len(x['cells'])==10 and x['cells'][4]==r['ref'] and x['cells'][2]==str(r['invoice']['EntryNumber']) and x['cells'][5].startswith('70 -')]
                if len(hits)!=1 or euro(hits[0]['cells'][6])!=money(r['amount']):set_exception(r,'own_invoice_not_fully_open_in_ui');persist();continue
                await toggle(frame,hits[0]);await frame.locator('#'+hits[0]['id']).locator('select').select_option('0');reader.selected_proof(r,await match_rows(frame))
                require(euro(await frame.locator('#Balance').input_value())==0 and euro(await frame.locator('#SelectedAmount').input_value())==money(r['amount']),'selection_amount_changed')
                require(validate_open(r,await orders([r['ref']])) is None,'last_api_precondition_changed')
                claim_save(conn,task,r,plan);summary['financial_saves_this_run']+=1;persist()
                await frame.locator('#btnSave').click()
                # Do not navigate away while Exact's save request is still in flight.
                try:
                    await frame.locator('#btnSave').wait_for(state='hidden',timeout=45000)
                except Exception:
                    # Successful Exact saves close/detach the matching window.
                    # Closure is only an acknowledgement; UI/API readback below
                    # still has to prove the saved own-invoice identity.
                    if not frame.is_detached() and not frame.page.is_closed():raise
                frame=await open_match(context,page,r);ui=await match_rows(frame);reader.selected_proof(r,ui,saved=True)
                r['evidence'].append({'at':now(),'phase':'ui_saved_pending_api','rows':ui});saved.append(r);persist()
        if saved:
            after=await api.rows('financialtransaction/TransactionLines',{'$filter':query,'$select':SELECT})
            opened=await orders([r['ref'] for r in saved])
            for r in saved:
                own=[x for x in after if x['ID'] in {r['bank_line_id'],r['offset_id']}]
                verify_group_readback(r,own,opened)
                r['evidence'].append({'at':now(),'phase':'readback','rows':r['evidence'][-1]['rows'],'open_items':[x for x in opened if x.get('YourRef')==r['ref']],'source_lines':own})
                r.update(state='matched_verified',workflow_status='decided',execution_status='verified');r['attempts'][-1]['outcome']='verified';persist()
        summary['state']='group_verified'
    except Exception as exc:summary.update(state='blocked',reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z0-9_]+',str(exc)) else type(exc).__name__)
    finally:
        if claimed:persist()
        if locked:conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
        conn.close()
    return summary
if __name__=='__main__':
    import sys
    mode=sys.argv[1];print(json.dumps(asyncio.run(run(mode if mode=='prepare' else int(mode)))))
