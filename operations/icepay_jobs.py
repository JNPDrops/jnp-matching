"""Explicit adapters for existing ICEPAY task identities; no new date ranges."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os

from operations import agent_jobs as jobs


@dataclass(frozen=True)
class Task:
    module: object
    task_id: str
    activation: str | None
    query: str
    success: frozenset
    with_app: bool = False

    @property
    def expires(self): return self.module.EXPIRES


def catalog():
    from operations import icepay_journal_task as journal, icepay_fetch_probe as fetch
    from operations import icepay_transactions as source, icepay_booking as booking
    from operations import icepay_apply as upload, icepay_matching as match
    result = [
        Task(journal,journal.TASK_ID,None,"SELECT result->>'status' FROM icepay_journal_tasks WHERE task_id=%s",frozenset({'inspected','already_exists'}),True),
        Task(fetch,fetch.PROBE_ID,fetch.ACTIVATION,"SELECT result->>'status' FROM icepay_fetch_probes WHERE probe_id=%s",frozenset({'passed'})),
        Task(source,source.JOB,source.ACTIVATION,"SELECT status->>'state' FROM icepay_transaction_tasks WHERE job=%s",frozenset({'downloaded'})),
        Task(booking,booking.PREPARE,'ICEPAY_TRANSACTION_TASK_ID',"SELECT summary->>'state' FROM icepay_booking_runs WHERE task=%s",frozenset({'prepared'})),
    ]
    for task_id in (upload.APPLY,upload.RECONCILE):
        result.append(Task(upload,task_id,'ICEPAY_TRANSACTION_TASK_ID',"SELECT summary->>'state' FROM icepay_receipt_import_runs WHERE task=%s",frozenset({'import_verified'})))
    for task_id in sorted(match.TASKS):
        result.append(Task(match,task_id,'ICEPAY_MATCH_TASK_ID',"SELECT summary->>'state' FROM icepay_matching_runs WHERE task=%s",frozenset({'prepared','group_verified','complete','incomplete','no_eligible_items'})))
    return {task.task_id:task for task in result}


def validate(job):
    task=catalog().get(job['task_key'])
    if task is None or job['action']!='run' or job['params']:
        raise ValueError('unknown_icepay_command')
    if datetime.now(timezone.utc)>=task.expires:
        raise ValueError('icepay_historical_authorization_expired')
    return task


def observed_outcome(app, task):
    with app._db_connect() as conn:
        row=conn.execute(task.query,(task.task_id,)).fetchone()
    if row is None or row[0] not in task.success:
        raise ValueError('icepay_task_has_no_confirmed_completion')


async def dispatch(app, job):
    task=validate(job)
    args=(app,) if task.with_app else ()
    # Explicit identity, never an environment override shared by other roles.
    await task.module.run(*args,task_id=task.task_id)
    await asyncio.to_thread(observed_outcome,app,task)


def seed_configured(conn, app):
    """Bridge existing activation flags once; old completed jobs are never reset."""
    jobs.initialize(conn)
    now=datetime.now(timezone.utc)
    for task in catalog().values():
        if now>=task.expires:continue
        if task.activation and os.environ.get(task.activation)!=task.task_id:continue
        if task.activation is None and os.environ.get(task.module.CREATE_ENV)==task.module.CREATE_ID:
            continue  # Never create a journal as part of this migration.
        try:
            result=jobs.submit(conn,app.DIVISION,'icepay',task.task_id,'run',{},
                'configured:'+task.task_id,task.expires)
            if result['state'] in {'queued','running','uncertain'}:break
        except jobs.JobConflict:break


async def serve(app):
    async def run(job): await dispatch(app,job)
    async def seed(): await asyncio.to_thread(jobs.database_call,app,seed_configured,app)
    await jobs.consume(app,'icepay',run,seed=seed)
