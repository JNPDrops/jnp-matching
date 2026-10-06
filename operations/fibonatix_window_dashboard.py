"""Publish this completed bounded run through the existing private dashboard feed.

No Exact/PSP write, no authorization change, no case-decision overwrite.
"""
import asyncio,copy,json
from operations.fibonatix_window_import import JOB,API,require
from operations.fibonatix_window_matching import load_plan,stats,now

def project(plan,orders,checked_at,opened):
    require(stats(plan)['pending']==0 and stats(plan)['unknown_saves']==0,'run_not_finished')
    byid={r['order_id']:r for r in orders};result=copy.deepcopy(plan)
    result['created_at']=plan['prepared_at'];result['final_readback']={'at':now(),'receivables':opened}
    for r in result['receipts']:
        order=byid.get(r['woo_id']);require(order and 'TD'+str(order['order_number']).lstrip('#')==r['ref'],'order_identity_changed')
        r.update(woo=r['woo_id'],entry_number=r['entry'],webshop_order=order,webshop_lookup_state='observed',webshop_checked_at=checked_at)
        if str(r['exception']).startswith('invoice_missing_webshop_'):
            r['original_exception']=r['exception'];r['exception']='invoice_missing';r['invoice_refresh']={'rows':r.get('invoice_candidates',[]),'at':checked_at}
        if r['state']=='exception':
            credits=[x for x in opened if str(x.get('JournalCode')).strip()=='26' and r['trx'] in str(x.get('Description') or '')]
            if len(credits)==1:r['state']='exception_verified'
    return result

async def publish():
    from app import main
    from operations.icepay_matching import Reader
    from operations.fibonatix_import import encode_evidence
    conn=main._db_connect()
    try:
        plan=load_plan(conn);require(stats(plan)['pending']==0 and stats(plan)['unknown_saves']==0,'run_not_finished')
        evidence,at=conn.execute('SELECT result,attempted_at FROM paragon_login_probes WHERE probe_id=%s',('fibonatix-order-evidence-20261003-05-v1',)).fetchone()
        refs=[r['ref'] for r in plan['receipts'] if r['state']=='exception'];api=API(main)
        opened=await Reader.orders(api,refs) if refs else []
        output=project(plan,evidence['orders'],at.isoformat(),opened)
        with conn.transaction():
            conn.execute('INSERT INTO fibonatix_import_artifacts(job,name,data) VALUES(%s,%s,%s::jsonb) ON CONFLICT(job,name) DO UPDATE SET data=EXCLUDED.data',(JOB,'strict_order_plan',json.dumps(encode_evidence(output))))
            conn.execute('UPDATE fibonatix_import_jobs SET data=data || %s::jsonb,updated_at=now() WHERE job=%s',(json.dumps({'status':'receipts_imported_matching_run_finished','matching':stats(plan),'dashboard_published_at':now()}),JOB))
        from app.dashboard.worklist import strict_cases
        cases=strict_cases(JOB,output,now())
        return {'state':'dashboard_feed_published','cases':len(cases),'resolved':sum(r['status']=='resolved' for r in cases),'open':sum(r['status']!='resolved' for r in cases),'api_calls':api.calls,'quota':api.quota}
    finally:conn.close()
if __name__=='__main__':print(json.dumps(asyncio.run(publish())))
