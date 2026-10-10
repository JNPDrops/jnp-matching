"""Fresh cutoff source acquisition through the existing ICEPAY integration.

Uses its verified account, date-filter pagination, export and UTC/UI comparison.
Each processing batch retains its own export. It never calls Exact or reuses a
historical activation flag, claim, expiry or completed source task.
"""
import asyncio
import base64
import hashlib
import json
from datetime import datetime,timezone

from operations import processing_imports as imports
from operations.worker_write_fence import current_owner


def payload(day,start,end,status,artifacts,summary,classify):
    imports.require(status.get('state')=='downloaded' and status.get('stage')=='complete','icepay_source_not_downloaded')
    proof=artifacts.get('proof',{})
    imports.require(proof.get('period_from')==proof.get('period_through')==day.isoformat(),'icepay_capture_day_changed')
    ids=proof.get('ui_payment_ids')
    imports.require(isinstance(ids,list) and len(ids)==len(set(ids)) and proof.get('ui_payment_count')==len(ids),'icepay_source_count_unverified')
    if ids:
        raw=base64.b64decode(artifacts.get('payments_csv',''),validate=True)
        imports.require(bool(raw) and hashlib.sha256(raw).hexdigest()==summary.get('source_sha256'),'icepay_export_digest_changed')
        imports.require(artifacts.get('timezone_proof',{}).get('verified') is True,'icepay_source_timezone_unverified')
        rows=artifacts.get('transactions',[])
        imports.require(len(rows)==len(ids) and {r['payment_id'] for r in rows}==set(ids),'icepay_source_identity_changed')
        selected=imports.bounded_rows(rows,start,end)
        candidates,exceptions=[],[]
        # Unknown fee/refund formats stay separate. An independently proved
        # successful positive receipt is not hidden behind one such exception.
        for row in selected:
            if row['status']!='OK':continue
            try:
                candidates.extend(classify(raw,[row]))
            except ValueError:
                exceptions.append({'payment_id':row['payment_id'],'reason':'source_item_policy_requires_review'})
    else:
        imports.require(summary.get('source_rows')==0,'icepay_empty_source_unverified')
        candidates,exceptions=[],[]
    imports.require(all(r['date']==day.isoformat() for r in candidates),'icepay_candidate_booking_day_changed')
    return {'window_start':start.isoformat(),'window_end':end.isoformat(),
        'candidates':candidates,'exceptions':exceptions,'captured_at':datetime.now(timezone.utc).isoformat(),
        'summary':summary,'source_sha256':summary.get('source_sha256'),
        'refunds':artifacts.get('refunds',{}),'artifacts':artifacts,
        'financial_writes':False}


def claim(app,key):
    owner=current_owner()
    imports.require(app.DIVISION==3977752 and owner is not None and owner.role=='icepay'
        and owner.division==3977752,'assigned_icepay_source_owner_required')
    with app._db_connect() as conn:
        imports.initialize(conn)
        imports.require(conn.execute('''SELECT 1 FROM jnp_worker_roles WHERE division=3977752
            AND role='icepay' AND lease_id=%s AND active_owner=%s AND NOT draining
            AND lease_until>clock_timestamp()''',(owner.lease_id,owner.owner)).fetchone(),
            'icepay_source_role_lease_unavailable')
        window=conn.execute('''SELECT processing_date,window_start,window_end FROM jnp_processing_batches
            WHERE division=3977752 AND batch_key=%s''',(key,)).fetchone()
        imports.require(window is not None and window[2]<=datetime.now(timezone.utc),'registered_closed_cutoff_required')
        added=conn.execute('''INSERT INTO jnp_processing_sources(batch_key,psp,state)
            VALUES(%s,'icepay','capturing') ON CONFLICT DO NOTHING RETURNING batch_key''',(key,)).fetchone()
        previous=conn.execute("SELECT state,data FROM jnp_processing_sources WHERE batch_key=%s AND psp='icepay'",(key,)).fetchone()
    if not added:
        imports.require(previous[0]=='verified','previous_source_attempt_requires_review')
        return window,previous[1]
    return window,None


def finish(app,key,state,data):
    with app._db_connect() as conn:
        changed=conn.execute('''UPDATE jnp_processing_sources SET state=%s,data=%s::jsonb,updated_at=now()
            WHERE batch_key=%s AND psp='icepay' AND state='capturing' RETURNING batch_key''',
            (state,json.dumps(data),key)).fetchone()
        imports.require(changed,'source_completion_identity_changed')


def evidence(data,existing=False):
    return {'state':'verified','already_captured':existing,'financial_writes':False,
        'selected_candidates':len(data['candidates']),'source_summary':data['summary'],
        'unprocessed_source_items':len(data['exceptions']),
        'unprocessed_refunds':data.get('refunds',{}).get('total',0),
        'refund_count_scope':'provider_day_requires_timestamp_review',
        'source_sha256':data.get('source_sha256')}


async def capture(app,job,stage):
    from operations.icepay_source_window import capture as acquire
    from operations.icepay_daily import classify_rows
    key=job['task_key']
    window,previous=await asyncio.to_thread(claim,app,key)
    if previous is not None:return evidence(previous,True)
    day,start,end=window
    artifacts={};status=None;summary={}
    try:
        status,artifacts,summary=await asyncio.wait_for(acquire(day,day),timeout=420)
        data=payload(day,start,end,status,artifacts,summary,classify_rows)
    except BaseException as exc:
        # Preserve a submitted read export and evidence; no silent new export
        # under the same identity on a subsequent process start.
        await asyncio.to_thread(finish,app,key,'blocked',{'artifacts':artifacts,'status':status,
            'summary':summary,'error_type':type(exc).__name__,'financial_writes':False})
        raise
    await asyncio.to_thread(finish,app,key,'verified',data)
    return evidence(data)
