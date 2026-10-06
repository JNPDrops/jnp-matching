"""Collect an already requested export; never submit an export or call Exact."""
import asyncio
import base64
from datetime import date
import json
import os
from operations import icepay_transactions as t, icepay_browser as b
from operations.icepay_source_window import validate_source
from operations.icepay_fetch_probe import environments
JOB='icepay-existing-export-20261004-05-v2'
SOURCE='icepay-source-20261004-20261005-v1'

async def collect(artifacts):
    from playwright.async_api import async_playwright
    base,_=environments(os.environ)
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=base['PLAYWRIGHT_BROWSERS_PATH']
    status={'financial_writes':False,'stage':'login','state':'started'}
    result={'source_job':SOURCE};summary={}
    try:
        proof=artifacts['proof']
        if (proof['period_from'],proof['period_through'])!=('2026-10-04','2026-10-05'):
            raise ValueError('wrong_source_period')
        ids=proof['ui_payment_ids']
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True,env=base)
            try:
                context=await browser.new_context(accept_downloads=True,service_workers='block',timezone_id='Europe/Amsterdam')
                await b.protect_requests(context)
                page=await context.new_page();page.set_default_timeout(15000)
                downloads=asyncio.Queue();page.on('download',downloads.put_nowait)
                auth=await b.authenticate(page,b.Credentials.from_env(os.environ))
                if not auth['account_verified']:raise ValueError('account_not_verified')
                status['stage']='open_notifications'
                await t.open_notifications(page)
                # The notification marker is the readiness signal. Networkidle
                # can time out on this live dashboard and proves nothing here.
                result['notices']=await t.export_notifications(page)
                result['controls']=await t.download_control_metadata(page)
                links=await t.csv_links(page)
                if not links or len(links)>5:raise t.AcquisitionStopped('export_ambiguous')
                result['candidate_csvs']=[];size=0
                for link in links.values():
                    status['stage']='download_existing'
                    await b.guard_page(page)
                    await link.click()
                    dl=await asyncio.wait_for(downloads.get(),25)
                    raw=await t.read_download(dl);size+=len(raw)
                    if size>t.MAX_BYTES:raise ValueError('artifact_too_large')
                    result['candidate_csvs'].append(base64.b64encode(raw).decode())
                    scoped,coverage=t.scope_csv(raw,ids)
                    if scoped is None:continue
                    rows,summary,tz=validate_source(scoped,ids,artifacts['table_evidence'],date(2026,10,4),date(2026,10,5))
                    result.update(payments_csv=base64.b64encode(scoped).decode(),source_export_csv=base64.b64encode(raw).decode(),transactions=rows,timezone_proof=tz,coverage=coverage,proof=proof)
                    status.update(state='downloaded',stage='complete')
                    return status,result,summary
                raise t.AcquisitionStopped('export_not_completed')
            finally:await browser.close()
    except Exception as exc:
        status.update(state='blocked',reason=str(exc) if isinstance(exc,t.AcquisitionStopped) else type(exc).__name__)
    return status,result,summary

def main():
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL']) as c:
        source=c.execute('SELECT status,artifacts FROM icepay_transaction_tasks WHERE job=%s',(SOURCE,)).fetchone()
        if not source or source[0].get('state')!='blocked':raise ValueError('source_not_blocked')
        claim=c.execute('INSERT INTO icepay_transaction_tasks(job,status) VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING job',(JOB,json.dumps({'state':'started','financial_writes':False}))).fetchone()
    if not claim:print('already_claimed');return
    status,artifacts,summary=asyncio.run(collect(source[1]))
    with psycopg.connect(os.environ['DATABASE_URL']) as c:
        c.execute('UPDATE icepay_transaction_tasks SET status=%s::jsonb,artifacts=%s::jsonb,summary=%s::jsonb WHERE job=%s',(json.dumps(status),json.dumps(artifacts),json.dumps(summary),JOB))
    print(json.dumps({'job':JOB,**status,'summary':summary,'downloaded_csvs':len(artifacts.get('candidate_csvs',[])),'notices':artifacts.get('notices'),'controls':artifacts.get('controls')}))
if __name__=='__main__':main()
