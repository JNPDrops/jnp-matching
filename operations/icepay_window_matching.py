"""Own-order matching only for the verified ICEPAY 4–5 October import.

Each group is claimed once, each bank line is claimed atomically before save.
Unknown saves halt all later groups. No write-off, cross-order match or repair.
"""
import asyncio,json,re
from collections import Counter
from operations.icepay_window_import import JOB,API,SELECT,reconcile,require

def approved_plan(plan,imported):
    require(imported[1].get('state')=='import_verified' and imported[1].get('verified_receipts')==25,'verified_import_required')
    require(plan['manifest']==imported[0]['manifest'] and len(plan['receipts'])==25,'plan_source_changed')
    byid={r['payment_id']:r for r in plan['manifest']}
    require(len(byid)==25 and len({r['bank_line_id'] for r in plan['receipts']})==25,'duplicate_source_identity')
    for r in plan['receipts']:
        require(r['payment_id'] in byid and all(r[k]==byid[r['payment_id']][k] for k in ['amount','ref','description','entry']),'receipt_source_changed')
        require(r['source_order']==r['ref'] and r['debtor']=='109419' and r['journal']=='27' and r['currency']=='EUR','receipt_scope_changed')
    require(not any(r['state']=='match_requested' for r in plan['receipts']),'unknown_save_requires_readback')

def claim_save(conn,task,receipt,plan):
    from operations.icepay_matching import now
    with conn.transaction():
        claimed=conn.execute('INSERT INTO icepay_matching_saves(bank_line_id,task) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING bank_line_id',(receipt['bank_line_id'],task)).fetchone()
        require(bool(claimed),'previous_save_reconcile_only')
        receipt.update(state='match_requested',execution_status='unknown_readback_required')
        receipt['attempts'].append({'at':now(),'task':task,'outcome':'unknown_readback_required'})
        conn.execute('UPDATE icepay_order_matching SET data=%s::jsonb,updated_at=now() WHERE job=%s',(json.dumps(plan),JOB))

async def run(group):
    from app import main
    from operations import icepay_matching as m
    from operations.strict_order_matching import session,match_rows,toggle,euro
    require(group in range(1,6),'group_out_of_scope')
    task=JOB+':match-group-'+str(group);conn=main._db_connect();locked=claimed=False;plan={}
    summary={'state':'started','financial_saves_this_run':0,'task':task}
    api=API(main)
    async def orders(refs,history=False):return await m.Reader.orders(api,refs,history=history)
    def persist():
        if plan:conn.execute('UPDATE icepay_order_matching SET data=%s::jsonb,updated_at=now() WHERE job=%s',(json.dumps(plan),JOB))
        summary.update({k:v for k,v in m.stats(plan).items() if k!='exceptions'},api_calls=api.calls,quota=api.quota)
        summary['exception_counts']=dict(Counter(r['exception'] for r in plan.get('receipts',[]) if r['exception']))
        conn.execute('UPDATE icepay_matching_runs SET summary=%s::jsonb WHERE task=%s',(json.dumps(summary),task))
    try:
        locked=conn.execute("SELECT pg_try_advisory_lock(hashtextextended('jnp:3977752:icepay:receipts',0))").fetchone()[0];require(locked,'another_icepay_writer')
        claimed=conn.execute('INSERT INTO icepay_matching_runs(task) VALUES(%s) ON CONFLICT DO NOTHING RETURNING task',(task,)).fetchone() is not None;require(claimed,'group_already_claimed')
        imported=conn.execute('SELECT artifacts,summary FROM icepay_receipt_import_runs WHERE task=%s',(JOB+':apply',)).fetchone()
        plan=conn.execute('SELECT data FROM icepay_order_matching WHERE job=%s',(JOB,)).fetchone()[0]
        approved_plan(plan,imported)
        queue=[r for r in plan['receipts'] if r['state']=='pending'][:5]
        if not queue:summary['state']='no_pending_items';return summary
        counts=Counter(r['source_order'] for r in plan['receipts'])
        async with session() as (context,page):
            for r in queue:
                summary['state']='processing';persist()
                history=await orders([r['source_order']],history=True);opened=await orders([r['source_order']])
                reason=m.classify(r,history,opened,counts)
                if reason:m.set_exception(r,reason);persist();continue
                ledger=await api.rows('financialtransaction/TransactionLines',{'$filter':"EntryID eq guid'"+r['entry_id']+"'",'$select':SELECT})
                require(reconcile([r],ledger)['complete'],'source_entry_changed')
                frame=await m.open_match(context,page,r);rows=await match_rows(frame)
                r['evidence'].append({'at':m.now(),'phase':'before','rows':rows,'open_items':opened})
                if any(x['checked'] for x in rows):m.set_exception(r,'existing_match_requires_inspection');persist();continue
                hits=[x for x in rows if len(x['cells'])==10 and x['cells'][4]==r['source_order'] and x['cells'][2]==str(r['invoice']['EntryNumber']) and x['cells'][5].startswith('70 -')]
                if len(hits)!=1 or euro(hits[0]['cells'][6])!=m.money(r['amount']):m.set_exception(r,'own_invoice_not_fully_open_in_ui');persist();continue
                await toggle(frame,hits[0]);await frame.locator('#'+hits[0]['id']).locator('select').select_option('0')
                m.selected_proof(r,await match_rows(frame))
                require(euro(await frame.locator('#Balance').input_value())==0 and euro(await frame.locator('#SelectedAmount').input_value())==m.money(r['amount']),'selection_amount_changed')
                require(m.validate_open(r,await orders([r['source_order']])) is None,'last_api_precondition_changed')
                claim_save(conn,task,r,plan);summary['financial_saves_this_run']+=1;persist()
                await frame.locator('#btnSave').click()
                await asyncio.sleep(2)
                frame=await m.open_match(context,page,r);selected=await match_rows(frame)
                m.selected_proof(r,selected,saved=True)
                after=await api.rows('financialtransaction/TransactionLines',{'$filter':"EntryID eq guid'"+r['entry_id']+"'",'$select':SELECT})
                require(reconcile([r],after)['complete'],'source_changed_after_save')
                opened=await orders([r['source_order']]);invoices,credits=m.open_rows(r,opened)
                require(not invoices and not credits,'saved_match_not_closed')
                r['evidence'].append({'at':m.now(),'phase':'readback','rows':selected,'open_items':opened,'source_lines':[l for l in after if l['ID'] in {r['bank_line_id'],r['offset_id']}]})
                r.update(state='matched_verified',workflow_status='decided',execution_status='verified');r['attempts'][-1]['outcome']='verified';persist()
        summary['state']='group_verified'
    except Exception as exc:
        reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z0-9_]+',str(exc)) else type(exc).__name__
        summary.update(state='blocked',reason=reason)
    finally:
        if claimed:persist()
        if locked:conn.execute("SELECT pg_advisory_unlock(hashtextextended('jnp:3977752:icepay:receipts',0))")
        conn.close()
    return summary

if __name__=='__main__':
    import sys
    print(json.dumps(asyncio.run(run(int(sys.argv[1])))))
