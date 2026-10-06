"""Scoped native Exact Automatically, owned by the existing Fibonatix worker."""
import asyncio
import json
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode, urlsplit
from operations import nightly_batches as n, worker_write_fence as fence, task_drain
from operations.fibonatix_daily_source import require

BANK='63305e6f-827d-4884-9df1-6ebbb5858a76'
NOTES='Fibonatix TD'


def checked(snapshot, allowed, first, last):
    from operations.fibonatix_import import statement_rows
    from operations.strict_order_matching import euro
    controls={c['id']:c for c in snapshot['controls'] if c.get('id')}
    require(controls.get('BankAccount',{}).get('value','').strip('{}').lower()==BANK,'wrong_bank')
    require(controls.get('Notes',{}).get('value')==NOTES,'missing_note_filter')
    require(controls.get('Status1',{}).get('checked') is True and controls.get('Status2',{}).get('checked') is False,'wrong_status_filter')
    require(controls.get('EntryDate_Selection',{}).get('value')=='1000','missing_date_range')
    for field,day in [('EntryDate_From',first),('EntryDate_To',last)]:
        require(datetime.strptime(controls[field]['value'].strip(),'%d-%m-%Y').date()==day,'wrong_date_range')
    result=[]
    for row in statement_rows(snapshot):
        cells=row['cells'];match=re.search(r'Fibonatix (TD[0-9]+) \| Woo ([0-9]+) \| Betaling ([A-Za-z0-9]{6,32})\b',row['note'])
        require(match is not None,'unrecognized_receipt')
        ref,woo,pid=match.groups();expected=allowed.get(pid)
        require(expected is not None,'receipt_not_authorized')
        booked=datetime.strptime(cells[1],'%d-%m-%Y').date()
        amount=euro(cells[4])-euro(cells[5])
        require(cells[3]=='EUR' and cells[10]=='EUR' and cells[7].startswith('1100 -') and cells[8].startswith('100100 -'),'wrong_receipt_account')
        require(first<=booked<=last and booked.isoformat()==expected['date'] and ref==expected['ref'] and int(woo)==expected['woo_id'] and amount>0 and amount==Decimal(expected['amount']),'receipt_evidence_mismatch')
        result.append({'payment_id':pid,'ref':ref,'amount':str(amount),'date':booked.isoformat()})
    require(len({r['payment_id'] for r in result})==len(result),'duplicate_receipt')
    return result


async def selection(page,allowed,first,last):
    from operations.fibonatix_import import ui_snapshot
    url='https://start.exactonline.nl/docs/CflStatementsToBeCompleted.aspx?'+urlencode({'_Division_':n.DIVISION,'BankAccount':'{'+BANK+'}'})
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    require(urlsplit(page.url).hostname=='start.exactonline.nl' and urlsplit(page.url).path.endswith('/CflStatementsToBeCompleted.aspx'),'wrong_page')
    async with page.expect_navigation(wait_until='load',timeout=60000):await page.get_by_text('Reset',exact=True).click()
    await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    await page.locator('#Status1').check();await page.locator('#Status2').uncheck()
    await page.locator('#EntryDate_Selection').select_option('1000')
    await page.locator('#EntryDate_From').fill(first.strftime('%d-%m-%Y'))
    await page.locator('#EntryDate_To').fill(last.strftime('%d-%m-%Y'))
    await page.locator('#Notes').fill(NOTES)
    for field in ('#GLAccountTypeCheckBoxList1','#GLAccountTypeCheckBoxList2','#GLAccountTypeCheckBoxList3'):await page.locator(field).check()
    async with page.expect_navigation(wait_until='load',timeout=60000):await page.locator('#Filter_btnApply').click()
    if await page.locator('#List_ps-select').count() and await page.locator('#List_ps-select').input_value()!='9999':
        async with page.expect_navigation(wait_until='load',timeout=60000):await page.locator('#List_ps-select').select_option('9999')
    snapshot=await ui_snapshot(page)
    return snapshot,checked(snapshot,allowed,first,last)


def persist(app,key,state,data):
    with app._db_connect() as conn:
        conn.execute('UPDATE jnp_fibonatix_automatic_runs SET state=%s,data=%s::jsonb,updated_at=now() WHERE task_key=%s',(state,json.dumps(data),key))


@fence.owned_operation('fibonatix')
async def click(app,key,page,allowed,first,last,data):
    require(not task_drain.requested(),'worker_draining')
    await asyncio.to_thread(persist,app,key,'click_requested',data)
    async def native():
        async with page.expect_navigation(wait_until='load',timeout=90000):await page.locator('#btnAutomatic').click()
    await fence.browser_save(app,'fibonatix',native)
    after,remaining=await selection(page,allowed,first,last)
    require({r['payment_id'] for r in remaining}<={r['payment_id'] for r in data['before_receipts']},'unexpected_receipt_after_action')
    data.update(after=after,remaining=remaining,remaining_open=len(remaining),matched=len(data['before_receipts'])-len(remaining),automatic_attempted=True)
    await asyncio.to_thread(persist,app,key,'completed',data)
    return {'state':'completed','open_before':len(data['before_receipts']),'remaining_open':len(remaining),'matched':data['matched']}


async def run(app,job):
    require(job['action']=='daily_automatically' and set(job['params'])=={'date'},'invalid_automatic_job')
    day=date.fromisoformat(job['params']['date']);key=n.identity(day,'fibonatix')+':automatically'
    require(job['task_key']==n.identity(day,'fibonatix') and app.DIVISION==n.DIVISION,'wrong_scope')
    require(fence.current_owner() is not None and fence.current_owner().role=='fibonatix','assigned_worker_required')
    with app._db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_fibonatix_automatic_runs(task_key text PRIMARY KEY,state text NOT NULL,data jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now())''')
        prior=conn.execute('SELECT state,data FROM jnp_fibonatix_automatic_runs WHERE task_key=%s',(key,)).fetchone()
        if prior:
            require(prior[0]=='completed','previous_automatic_requires_review')
            return {'state':'completed','already_completed':True}
        imported=conn.execute('SELECT state,data FROM jnp_fibonatix_daily_imports WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchone()
        require(imported and imported[0]=='verified','daily_import_unfinished')
        states=dict(conn.execute('SELECT stage,state FROM jnp_nightly_stages WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchall())
        require(all(states.get(s) in n.TERMINAL for s in n.STAGES[:6]),'prior_day_work_unfinished')
        allowed={r['payment_id']:r for r in imported[1]['candidates']}
        # The already verified catch-up batch is authorized, never reimported.
        past=conn.execute('SELECT data FROM fibonatix_import_artifacts WHERE job=%s AND name=%s',('FIBO-20261003-05-AMSTERDAM','apply')).fetchone()
        require(past and past[0]['summary']['state']=='import_verified','catchup_import_not_verified')
        for row in past[0]['artifacts']['manifest']:
            pid=row.get('payment_id') or row['trx']
            require(pid not in allowed,'overlapping_source_ids')
            allowed[pid]=row
        first=min(date.fromisoformat(r['date']) for r in allowed.values())
        require(first<=day,'invalid_date_scope')
        conn.execute("INSERT INTO jnp_fibonatix_automatic_runs(task_key,state,data) VALUES(%s,'inspecting','{}'::jsonb)",(key,))
    from operations.strict_order_matching import session
    async with session() as (context,page):
        before,receipts=await selection(page,allowed,first,day)
        data={'before':before,'before_receipts':receipts,'first':first.isoformat(),'last':day.isoformat(),'policy':'Exact Automatically only'}
        if not receipts:
            data.update(remaining=[],remaining_open=0,matched=0,automatic_attempted=False)
            await asyncio.to_thread(persist,app,key,'completed',data)
            return {'state':'completed','open_before':0,'remaining_open':0,'matched':0}
        return await click(app,key,page,allowed,first,day,data)
