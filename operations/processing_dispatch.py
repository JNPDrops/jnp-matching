"""Strict command boundary for the processing chain.

Only installed adapters can run. Unsupported stages commit an explicit blocked
outcome; they never silently succeed. Activation must require complete adapter
coverage and provider preflight before enabling the coordinator.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone

from operations import processing_orchestrator as coordinator
from operations import processing_imports as imports
from operations import processing_completion as completion
from operations import routing_completion
from operations import task_drain, agent_jobs
from operations.worker_write_fence import current_owner
from operations.processing_icepay_source import capture as icepay_source


async def import_stage(app,job,stage):
    psp=stage.removesuffix('_import')
    fn=imports.fibonatix if psp=='fibonatix' else imports.icepay
    # An RF is processed only after its original receipt is independently
    # verified. It is never included in the positive receipt native selection.
    parts={'receipts':await fn(app,job['task_key'])}
    if psp=='fibonatix':
        parts['refunds']=await fn(app,job['task_key'],'refunds')
    return {'state':'verified','parts':parts,'matching_executed':False}


def boundary(app,key):
    with app._db_connect() as conn:
        row=conn.execute('''SELECT window_end FROM jnp_processing_batches
            WHERE division=3977752 AND batch_key=%s''',(key,)).fetchone()
        if not row:raise ValueError('registered_processing_batch_required')
        return row[0]


def completed_work(app,stage,cutoff):
    with app._db_connect() as conn:
        if stage=='routing':
            try:
                return routing_completion.require_recent(conn,cutoff)
            except routing_completion.RoutingNotReady:
                return None
        return completion.proof(conn,{'woo_rules':'woo-rules','tax':'tax','maintenance':'maintenance'}[stage],cutoff)


async def continuous_stage(app,job,stage):
    cutoff=await asyncio.to_thread(boundary,app,job['task_key'])
    # Observe the actual existing owner; never start another rule writer.
    deadline=datetime.now(timezone.utc)+timedelta(minutes=75)
    while not task_drain.requested():
        proof=await asyncio.to_thread(completed_work,app,stage,cutoff)
        if proof:return {'state':'verified','completion':proof}
        if datetime.now(timezone.utc)>=deadline:
            raise ValueError('relevant_worker_boundary_not_completed')
        await task_drain.wait(5)
    raise ValueError('worker_draining')


async def report_stage(app,job,stage):
    from operations.processing_reports import create
    return await asyncio.to_thread(create,app,job)


# This registry deliberately does not claim a source/native adapter is ready
# merely because a historical one-off module exists. Readiness exposes gaps.
ADAPTERS={
    'icepay_source':icepay_source,
    'fibonatix_import':import_stage,'icepay_import':import_stage,
    'routing':continuous_stage,'woo_rules':continuous_stage,
    'tax':continuous_stage,'maintenance':continuous_stage,'report':report_stage,
}


def missing_adapters():
    return [stage for stage,_ in coordinator.STAGES if stage not in ADAPTERS]


def validate(app,job):
    if app.DIVISION!=3977752 or job['action']!='processing_stage' or set(job['params'])!={'stage'}:
        raise ValueError('invalid_processing_command')
    stage=job['params']['stage']
    role=dict(coordinator.STAGES).get(stage)
    owner=current_owner()
    if role is None or owner is None or owner.role!=role or owner.division!=3977752:
        raise ValueError('processing_stage_owner_mismatch')
    with app._db_connect() as conn:
        row=conn.execute('''SELECT s.state,s.job_id FROM jnp_processing_stages s
            JOIN jnp_processing_batches b ON b.batch_key=s.batch_key
            WHERE b.division=3977752 AND s.batch_key=%s AND s.stage=%s''',
            (job['task_key'],stage)).fetchone()
        if not row or row[0]!='queued' or str(row[1])!=str(job['job_id']):
            raise ValueError('processing_stage_claim_identity_changed')
    return stage


def save_outcome(app,job,stage,state,evidence):
    with app._db_connect() as conn:
        coordinator.outcome(conn,job['task_key'],stage,state,evidence,job['job_id'])


def import_may_have_written(app,job,stage):
    if not stage.endswith('_import'):return False
    with app._db_connect() as conn:
        if not conn.execute("SELECT to_regclass('jnp_processing_imports')").fetchone()[0]:return False
        return conn.execute('''SELECT 1 FROM jnp_processing_imports WHERE batch_key=%s
            AND psp=%s AND write_requested AND state<>'verified' LIMIT 1''',
            (job['task_key'],stage.removesuffix('_import'))).fetchone() is not None


async def run(app,job):
    stage=await asyncio.to_thread(validate,app,job)
    try:
        adapter=ADAPTERS.get(stage)
        if adapter is None:raise ValueError('processing_stage_adapter_not_installed')
        evidence=await adapter(app,job,stage)
        if evidence.get('state')!='verified':
            raise ValueError('processing_stage_not_verified')
        await asyncio.to_thread(save_outcome,app,job,stage,'verified',evidence)
    except asyncio.CancelledError:
        await asyncio.to_thread(save_outcome,app,job,stage,'uncertain',{'reason':'worker_interrupted'})
        raise
    except Exception as exc:
        code=str(exc) if type(exc) is ValueError and re.fullmatch('[a-z][a-z_0-9]{1,79}',str(exc)) else 'processing_adapter_failed'
        uncertain=await asyncio.to_thread(import_may_have_written,app,job,stage)
        await asyncio.to_thread(save_outcome,app,job,stage,'uncertain' if uncertain else 'blocked',{'reason':code,'error_type':type(exc).__name__})
        raise
