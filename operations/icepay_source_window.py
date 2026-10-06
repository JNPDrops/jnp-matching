"""Read-only ICEPAY source capture with a fresh durable identity per window.

Run inside Render. Never calls Exact or imports/matches money. No modification of
legacy activation, expiry, source proof, claims or dossiers. Raw exports remain
in the private database. CLI emits only an aggregate status.
"""
import argparse
import asyncio
import base64
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import os
import re
import sys

from operations import icepay_transactions as t
from operations import icepay_browser as b
from operations.icepay_fetch_probe import environments


def identity(start, end):
    # These calendar controls have only been established for October 2026.
    if not date(2026, 10, 4) <= start <= end <= date(2026, 10, 5):
        raise ValueError('unauthorized_source_period')
    return f'icepay-source-{start:%Y%m%d}-{end:%Y%m%d}-v1'


def claim(conn, job):
    return conn.execute('''INSERT INTO icepay_transaction_tasks(job,status)
        VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING job''',
        (job,json.dumps({'state':'started','stage':'source_capture',
                        'financial_writes':False}))).fetchone() is not None


def finish(conn, job, status, artifacts, summary):
    conn.execute('''UPDATE icepay_transaction_tasks SET status=%s::jsonb,
        artifacts=artifacts || %s::jsonb,summary=%s::jsonb WHERE job=%s''',
        (json.dumps(status),json.dumps(artifacts),json.dumps(summary),job))


def resume_before_login(conn, job):
    # Explicit operator recovery only: once, before any login or source access.
    return conn.execute('''UPDATE icepay_transaction_tasks
        SET artifacts=artifacts || jsonb_build_object('before_login_failure',status),
            status='{"state":"started","stage":"source_capture","financial_writes":false}'::jsonb
        WHERE job=%s AND status->>'state'='blocked'
          AND status->>'stage'='browser_launch' AND status->>'reason'='Error'
          AND artifacts='{}'::jsonb AND summary='{}'::jsonb
        RETURNING job''',(job,)).fetchone() is not None


def collect_pending_export(conn, job):
    # Recover the submitted read export without submitting another export.
    row=conn.execute('''UPDATE icepay_transaction_tasks
        SET artifacts=artifacts || jsonb_build_object('export_pending_failure',status),
            status='{"state":"started","stage":"collect_existing_export","financial_writes":false}'::jsonb
        WHERE job=%s AND status->>'state'='blocked'
          AND status->>'stage'='payments_export'
          AND status->>'reason'='export_not_completed'
          AND artifacts->>'export_state'='submitted'
          AND artifacts ? 'proof' AND NOT artifacts ? 'export_pending_failure'
        RETURNING artifacts''',(job,)).fetchone()
    return row[0] if row else None


async def all_evidence(page):
    # Capture every filtered page and independently check row identities.
    combined, seen, headers = [], set(), None
    for _ in range(200):
        snap = await t.stable_payment_page(page)
        tables = await t.table_evidence(page)
        if len(tables) != 1:
            raise t.AcquisitionStopped('table_not_verified')
        current = tables[0]
        if headers is None:
            headers = current['headers']
        if headers != current['headers']:
            raise t.AcquisitionStopped('table_not_verified')
        ids = []
        for row in current['rows']:
            match = re.fullmatch(r'Select/deselect item (\d+) for bulk actions\.',row['id'])
            if not match or match[1] in seen:
                raise t.AcquisitionStopped('duplicate_payment')
            ids.append(match[1]); seen.add(match[1])
        if set(ids) != set(snap['ids']):
            raise t.AcquisitionStopped('count_mismatch')
        combined.extend(current['rows'])
        if len(seen)>5000:
            raise t.AcquisitionStopped('artifact_too_large')
        if not snap['next']:
            return sorted(seen), {'payments':[{'headers':headers,'rows':combined}]}
        await b.click_unique_read_control(page,re.compile(r'^Next$'))
        for _ in range(30):
            if (await t.stable_payment_page(page))['ids'] != snap['ids']:
                break
            await asyncio.sleep(.2)
        else:
            raise t.AcquisitionStopped('table_not_verified')
    raise t.AcquisitionStopped('artifact_too_large')


def validate_source(raw, ids, evidence, start, end):
    proof = t.verify_csv_timezone(raw,evidence,ids)
    if not proof['verified']:
        raise t.AcquisitionStopped('invalid_payment_date')
    rows,summary = t.parse_payments(raw,len(ids),ids,utc_to_amsterdam=True,start=start,end=end)
    summary.update(period_from=start.isoformat(),period_through=end.isoformat(),
                   timezone='Europe/Amsterdam',source_sha256=hashlib.sha256(raw).hexdigest())
    return rows,summary,proof


async def capture(start,end,existing=None):
    from playwright.async_api import async_playwright
    base,_ = environments(os.environ)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = base['PLAYWRIGHT_BROWSERS_PATH']
    artifacts,summary = dict(existing or {}),{}
    stage='configuration'
    try:
        credentials=b.Credentials.from_env(os.environ)
        async with async_playwright() as playwright:
            stage='browser_launch'
            browser=await playwright.chromium.launch(headless=True,env=base)
            try:
                context=await browser.new_context(accept_downloads=True,service_workers='block',timezone_id='Europe/Amsterdam')
                await b.protect_requests(context)
                page=await context.new_page(); page.set_default_timeout(15000)
                downloads=asyncio.Queue(); page.on('download',downloads.put_nowait)
                stage='login'
                login=await b.authenticate(page,credentials)
                if not login['account_verified']:
                    raise t.AcquisitionStopped('login_failed')
                stage='payments_filter'
                if existing:
                    proof=existing['proof']
                    if proof['period_from']!=start.isoformat() or proof['period_through']!=end.isoformat():
                        raise t.AcquisitionStopped('configuration')
                    ids,evidence=proof['ui_payment_ids'],existing['table_evidence']
                else:
                    await t.open_account_page(page,'Payments')
                    await t.apply_period(page,start=start,end=end)
                    ids,evidence=await all_evidence(page)
                    artifacts.update(proof={'period_from':start.isoformat(),'period_through':end.isoformat(),
                                        'ui_payment_ids':ids,'ui_payment_count':len(ids)},table_evidence=evidence)
                stage='payments_export'
                if ids:
                    raw=await (t.resume_payment_export(page,downloads,ids,artifacts) if existing else
                               t.legacy_payment_export(page,downloads,ids,artifacts))
                    artifacts['payments_csv']=base64.b64encode(raw).decode()
                    rows,summary,proof=validate_source(raw,ids,evidence,start,end)
                    artifacts.update(transactions=rows,timezone_proof=proof)
                else:
                    summary={'source_rows':0,'td_ok_count':0,'td_ok_total':'0.00',
                             'period_from':start.isoformat(),'period_through':end.isoformat()}
                stage='refunds'
                artifacts['refunds']=await t.read_refunds(page,start=start,end=end)
                summary['refund_rows']=artifacts['refunds']['total']
                return {'state':'downloaded','stage':'complete','financial_writes':False},artifacts,summary
            finally:
                await browser.close()
    except Exception as exc:
        reason=str(exc) if isinstance(exc,(t.AcquisitionStopped,b.Stopped)) else type(exc).__name__
        if not re.fullmatch(r'[a-zA-Z_]{1,60}',reason): reason='runtime_error'
        return {'state':'blocked','stage':stage,'reason':reason,'financial_writes':False},artifacts,summary


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--from-date',required=True,type=date.fromisoformat)
    parser.add_argument('--through-date',required=True,type=date.fromisoformat)
    recovery=parser.add_mutually_exclusive_group()
    recovery.add_argument('--resume-before-login',action='store_true')
    recovery.add_argument('--collect-existing-export',action='store_true')
    args=parser.parse_args(); job=identity(args.from_date,args.through_date)
    import psycopg
    # Commit claim BEFORE any remote access. A failed or interrupted claim must
    # be investigated; this command never retries it or chooses another ID.
    existing=None
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        if args.collect_existing_export:
            existing=collect_pending_export(conn,job); claimed=existing is not None
        else:
            claimed=resume_before_login(conn,job) if args.resume_before_login else claim(conn,job)
    if not claimed:
        print(json.dumps({'job':job,'state':'already_claimed','financial_writes':False})); return
    try:
        result,artifacts,summary=asyncio.run(asyncio.wait_for(capture(args.from_date,args.through_date,existing),timeout=420))
    except Exception as exc:
        result,artifacts,summary={'state':'blocked','stage':'runtime','reason':type(exc).__name__,'financial_writes':False},{},{}
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        finish(conn,job,result,artifacts,summary)
    print(json.dumps({'job':job,**result,'summary':summary},sort_keys=True))


if __name__=='__main__':
    main()
