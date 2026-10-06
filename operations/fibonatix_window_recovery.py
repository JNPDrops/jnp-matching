"""Read-only recovery of previously claimed saves. Never repeats a save.

A fully open source/invoice becomes an explicit no-retry exception. Any other
unproved outcome retains match_requested and prevents subsequent groups.
"""
import asyncio,json,re
from operations.fibonatix_window_import import JOB,LOCK,API,SELECT,require,reconcile
from operations.fibonatix_window_matching import load_plan,save_plan,stats,now,validate_open,verify_group_readback

def resolve(r,rows,lines,opened,selected_proof):
    require(reconcile([r],lines)['complete'],'source_identity_changed')
    if any(x['checked'] for x in rows):
        selected_proof(r,rows,saved=True);verify_group_readback(r,lines,opened)
        return 'matched_verified'
    require(validate_open(r,opened) is None,'outcome_still_unknown')
    return 'not_applied_verified'

async def recover(name):
    from app import main
    from operations import icepay_matching as reader
    from operations.strict_order_matching import session,open_match,match_rows
    require(re.fullmatch(r'readback_group_\d{2}_v1',name),'invalid_recovery_identity')
    conn=main._db_connect();locked=claimed=False;data={'financial_writes':False,'results':[]};summary={'state':'started','financial_writes':False}
    try:
        locked=conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0];require(locked,'another_fibonatix_writer')
        claimed=conn.execute('INSERT INTO fibonatix_import_attempts(job,action) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING action',(JOB,name)).fetchone() is not None;require(claimed,'recovery_already_claimed')
        plan=load_plan(conn);queue=[r for r in plan['receipts'] if r['state']=='match_requested'];require(0<len(queue)<=5,'unexpected_unknown_count')
        api=API(main);opened=await reader.Reader.orders(api,[r['ref'] for r in queue]);ids=[r[k] for r in queue for k in ['bank_line_id','offset_id']]
        ledger=await api.rows('financialtransaction/TransactionLines',{'$filter':' or '.join("ID eq guid'"+v+"'" for v in ids),'$select':SELECT})
        async with session() as (context,page):
            for r in queue:
                item={'trx':r['trx'],'at':now()}
                try:
                    frame=await open_match(context,page,r);rows=await match_rows(frame);lines=[l for l in ledger if l['ID'] in {r['bank_line_id'],r['offset_id']}]
                    item.update(rows=rows,source_lines=lines,open_items=[x for x in opened if x.get('YourRef')==r['ref']])
                    outcome=resolve(r,rows,lines,opened,reader.selected_proof)
                    r['evidence'].append({'at':now(),'phase':name,**item});r['attempts'][-1]['outcome']=outcome+'_by_readonly_recovery'
                    if outcome=='matched_verified':r.update(state=outcome,workflow_status='decided',execution_status='verified')
                    else:r.update(state='exception',exception='save_not_applied_requires_review',execution_status='not_applied_verified',workflow_status='open',suggested_action='Original save claim retained; fully open in Exact. Investigate; no automatic retry.')
                    item['state']=outcome;save_plan(conn,plan)
                except Exception as exc:item.update(state='still_unknown',error_type=type(exc).__name__)
                data['results'].append(item)
        summary.update(stats(plan),state='readback_complete' if not stats(plan)['unknown_saves'] else 'unknown_requires_review',api_calls=api.calls,quota=api.quota)
    except Exception as exc:summary.update(state='blocked',error_type=type(exc).__name__)
    finally:
        if claimed:conn.execute('INSERT INTO fibonatix_import_artifacts(job,name,data) VALUES(%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',(JOB,name,json.dumps({'summary':summary,**data})))
        if locked:conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
        conn.close()
    return summary
if __name__=='__main__':
    import sys
    print(json.dumps(asyncio.run(recover(sys.argv[1]))))
