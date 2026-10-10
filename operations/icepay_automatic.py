"""ICEPAY receipts are matched only by Exact's native Automatically action.

Standing instruction from Jasper, 6 October 2026. This module cannot create
journal entries, cross-order offsets or manual matches. Unmatched receipts stay
open. A new queue identity is used each round; an uncertain click is never retried.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import re
from urllib.parse import urlencode, urlsplit

from operations import agent_jobs, task_drain, worker_write_fence as fence

TASK = 'icepay-automatically-v1'
DIVISION = 3977752
BANK = 'd4b87c16-4c50-43c4-9c35-b9d74dba204d'
BASE = 'https://start.exactonline.nl'
NOTES = 'ICEPAY TD'
INTERVAL_SECONDS = 900
POLICY = {'matching': 'exact_automatically', 'cross_order_offset_entries': False,
          'manual_matching': False, 'unmatched': 'leave_open'}
LOG = logging.getLogger('uvicorn.error')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_icepay_automatic_runs (
        job_id UUID PRIMARY KEY, state TEXT NOT NULL,
        summary JSONB NOT NULL, evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


def automatic_blocked(conn):
    """Keep native matching paused after a proven cross-order match.

    Import jobs and the 900-second cadence are unaffected. Clearing the block
    requires an explicit reviewed resolution in the original durable evidence.
    """
    return conn.execute("""SELECT 1 FROM jnp_icepay_automatic_runs
        WHERE evidence->>'order_mismatch_confirmed'='true'
          AND COALESCE(evidence->>'order_mismatch_resolved','false')<>'true'
        LIMIT 1""").fetchone() is not None


def require_native_allowed(app):
    with app._db_connect() as conn:
        initialize(conn)
        require(not automatic_blocked(conn), 'native_order_mismatch_requires_review')


def seed(conn, app):
    """Runs on the existing assigned ICEPAY worker, including after restarts."""
    require(app.DIVISION == DIVISION, 'wrong_administration')
    initialize(conn)
    if automatic_blocked(conn):
        return
    # An unfinished native action must be reviewed, even after a restart.
    if conn.execute("SELECT 1 FROM jnp_icepay_automatic_runs WHERE state='click_requested' LIMIT 1").fetchone():
        return
    last = conn.execute('''SELECT created_at FROM jnp_agent_jobs
        WHERE division=%s AND role='icepay' AND task_key=%s
        ORDER BY created_at DESC LIMIT 1''', (DIVISION, TASK)).fetchone()
    now = datetime.now(timezone.utc)
    if last and (now - last[0]).total_seconds() < INTERVAL_SECONDS:
        return
    try:
        agent_jobs.submit(conn, DIVISION, 'icepay', TASK, 'run', {},
                          TASK + ':' + str(int(now.timestamp()) // INTERVAL_SECONDS),
                          now + timedelta(hours=1))
    except agent_jobs.JobConflict:
        return


def validate(job):
    require(job['task_key'] == TASK and job['action'] == 'run' and job['params'] == {},
            'invalid_icepay_automatic_command')


def persist(app, job_id, state, summary, evidence):
    with app._db_connect() as conn:
        conn.execute('''INSERT INTO jnp_icepay_automatic_runs(job_id,state,summary,evidence)
            VALUES(%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT(job_id) DO UPDATE SET state=EXCLUDED.state,
            summary=EXCLUDED.summary,evidence=EXCLUDED.evidence,updated_at=NOW()''',
            (job_id, state, json.dumps(summary), json.dumps(evidence)))


def checked_rows(snapshot):
    from operations.fibonatix_import import statement_rows
    from operations.strict_order_matching import euro
    controls = {c['id']: c for c in snapshot['controls'] if c.get('id')}
    require(controls.get('BankAccount', {}).get('value', '').strip('{}').lower() == BANK,
            'wrong_bank_account')
    require(controls.get('Notes', {}).get('value') == NOTES, 'receipt_filter_missing')
    require(controls.get('Status1', {}).get('checked') is True and
            controls.get('Status2', {}).get('checked') is False, 'open_status_filter_missing')
    rows = statement_rows(snapshot)
    receipts = []
    for row in rows:
        cells = row['cells']
        match = re.search(r'\bICEPAY (TD\d+) \| Payment (\d+)\b', row['note'])
        require(match is not None, 'not_an_icepay_order_receipt')
        require(cells[3] == 'EUR' and cells[10] == 'EUR', 'unexpected_currency')
        require(cells[7].startswith('1100 -') and cells[8].startswith('109419 -'),
                'unexpected_receipt_account')
        amount = euro(cells[4]) - euro(cells[5])
        require(amount > Decimal(0), 'not_a_positive_receipt')
        receipts.append({'order': match[1], 'payment_id': match[2], 'amount': str(amount)})
    require(len({r['payment_id'] for r in receipts}) == len(receipts), 'duplicate_receipt_rows')
    return receipts


async def open_receipts(page):
    from operations.fibonatix_import import ui_snapshot
    url = BASE + '/docs/CflStatementsToBeCompleted.aspx?' + urlencode({
        '_Division_': DIVISION, 'BankAccount': '{' + BANK + '}'})
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    require(urlsplit(page.url).hostname == 'start.exactonline.nl' and
            urlsplit(page.url).path.endswith('/CflStatementsToBeCompleted.aspx'),
            'unexpected_statement_page')
    # Exact remembers filters from a user's earlier session. Reset them before
    # applying this worker's explicit scope, including amount/account filters.
    async with page.expect_navigation(wait_until='load', timeout=60000):
        await page.get_by_text('Reset', exact=True).click()
    # Reset clears Own account too. Re-enter the explicit bank URL afterwards.
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    await page.locator('#Status1').check()
    await page.locator('#Status2').uncheck()
    await page.locator('#EntryDate_Selection').select_option('1100')
    await page.locator('#Notes').fill(NOTES)
    for field in ('#GLAccountTypeCheckBoxList1', '#GLAccountTypeCheckBoxList2', '#GLAccountTypeCheckBoxList3'):
        await page.locator(field).check()
    async with page.expect_navigation(wait_until='load', timeout=60000):
        await page.locator('#Filter_btnApply').click()
    if await page.locator('#List_ps-select').count() and await page.locator('#List_ps-select').input_value() != '9999':
        async with page.expect_navigation(wait_until='load', timeout=60000):
            await page.locator('#List_ps-select').select_option('9999')
    snapshot = await ui_snapshot(page)
    return snapshot, checked_rows(snapshot)


@fence.owned_operation('icepay')
async def automatic_click(app, job, page, before, summary):
    require(not task_drain.requested(), 'worker_draining')
    require_native_allowed(app)
    from operations import routing_completion
    routing_proof=await asyncio.to_thread(routing_completion.check_app,app,None,
        [r['order'] for r in checked_rows(before)])
    evidence = {'before': before, 'routing_before_automatically': routing_proof}
    summary.update(automatic_attempted=True)
    await asyncio.to_thread(persist, app, job['job_id'], 'click_requested', summary, evidence)
    async def submit_native():
        async with page.expect_navigation(wait_until='load', timeout=90000):
            await page.locator('#btnAutomatic').click()
    await fence.browser_save(app, 'icepay', submit_native)
    after, remaining = await open_receipts(page)
    evidence['after'] = after
    summary.update(state='completed', remaining_open=len(remaining),
                   remaining_receipts=remaining)
    await asyncio.to_thread(persist, app, job['job_id'], 'completed', summary, evidence)
    LOG.warning('ICEPAY_AUTOMATIC completed open_before=%s remaining_open=%s',
                summary.get('open_before'), len(remaining))
    return summary


async def run(app, job):
    validate(job)
    require_native_allowed(app)
    require(app.DIVISION == DIVISION and app.BASE_URL == BASE, 'wrong_administration')
    require(fence.current_owner() is not None and fence.current_owner().role == 'icepay',
            'assigned_icepay_worker_required')
    from operations.strict_order_matching import session
    summary = {'state': 'inspecting', 'policy': POLICY, 'automatic_attempted': False,
               'journal_entries_created_by_agent': 0}
    await asyncio.to_thread(persist, app, job['job_id'], 'inspecting', summary, {})
    try:
        async with session() as (context, page):
            before, receipts = await open_receipts(page)
            summary['open_before'] = len(receipts)
            if not receipts:
                summary.update(state='completed', remaining_open=0)
                await asyncio.to_thread(persist, app, job['job_id'], 'completed', summary, {'before': before})
                LOG.warning('ICEPAY_AUTOMATIC completed open_before=0 remaining_open=0')
                return summary
            return await automatic_click(app, job, page, before, summary)
    except Exception as exc:
        # Preserve the durable before/after evidence and unresolved intent once
        # a native action might have started. Never turn it into a fresh retry.
        if not summary['automatic_attempted']:
            summary.update(state='blocked', error_type=type(exc).__name__)
            await asyncio.to_thread(persist, app, job['job_id'], 'blocked', summary, {})
        LOG.warning('ICEPAY_AUTOMATIC blocked error_type=%s automatic_attempted=%s',
                    type(exc).__name__, summary['automatic_attempted'])
        raise

