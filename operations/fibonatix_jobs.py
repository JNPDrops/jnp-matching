"""Fixed Fibonatix command adapters; preserves batch scope and authorization."""
from uuid import uuid4
from fastapi import HTTPException
from operations import agent_jobs as jobs

READ_ACTIONS={'preflight','reconcile','inspect','inspect_all','inspect_match','audit_order_matches'}
STRICT_ACTIONS={'prepare','inspect','undo','undo_orphans','match','correct','recover','verify',
                'diagnose_closed','history_pending','refresh_missing'}


def validate(action, params):
    if action.startswith('strict_') and action[7:] in STRICT_ACTIONS:
        if set(params)!={'limit'} or type(params['limit']) is not int or not 1<=params['limit']<=5:
            raise ValueError('invalid_strict_command')
    elif action in READ_ACTIONS|{'import'}:
        if params:raise ValueError('unexpected_command_params')
    else:
        raise ValueError('retired_or_unknown_fibonatix_command')


def submit(request, action, params=None):
    from operations import fibonatix_import as legacy
    params={} if params is None else params
    validate(action,params)
    legacy.state()  # require the existing prepared dossier
    with legacy.database() as conn:
        jobs.initialize(conn)
        try:
            return jobs.submit(conn,legacy.DIVISION,'fibonatix',legacy.JOB,action,params,
                request.headers.get('idempotency-key') or str(uuid4()),legacy.EXPIRES)
        except jobs.JobConflict as exc:
            raise HTTPException(409,str(exc)) from None


async def dispatch(job):
    if job['action']=='daily_review':
        from app import main as app
        from operations.fibonatix_daily_review import run
        return await run(app,job)
    if job['action']=='daily_automatically':
        from app import main as app
        from operations.fibonatix_daily_automatic import run
        return await run(app,job)
    if job['action'] in {'daily_prepare','daily_import','daily_reconcile','daily_followup_prepare','daily_followup_import','daily_followup_reconcile'}:
        from app import main as app
        from operations.fibonatix_daily import run
        return await run(app,job)
    from operations import fibonatix_import as legacy
    from datetime import datetime, timezone
    validate(job['action'],job['params'])
    if job['task_key']!=legacy.JOB or datetime.now(timezone.utc)>=legacy.EXPIRES:
        raise ValueError('fixed_batch_authorization_expired_or_changed')
    action=job['action']
    if action.startswith('strict_'):
        from operations.strict_order_matching import run
        return await run(action[7:],job['params']['limit'])
    if action=='audit_order_matches':
        from operations.source_order_policy import audit
        return await audit()
    return await legacy.run(action)


async def serve(app):
    import asyncio,time
    from operations.fibonatix_daily_review import seed
    next_seed=0
    async def daily_seed():
        nonlocal next_seed
        if time.monotonic()<next_seed:return
        next_seed=time.monotonic()+60
        try:
            await asyncio.to_thread(seed,app)
        except jobs.JobConflict:
            pass  # another command won the role queue; retry the audit later
    await jobs.consume(app,'fibonatix',dispatch,seed=daily_seed)


def status():
    from operations import fibonatix_import as legacy
    with legacy.database() as conn:
        conn.execute('SET default_transaction_read_only=on')
        return jobs.snapshot(conn,legacy.DIVISION,'fibonatix')
