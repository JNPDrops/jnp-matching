"""Read-only historical report/probe jobs with durable completion and expiry."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os

from operations import agent_jobs as jobs

@dataclass(frozen=True)
class Task:
    module: object
    task_id: str
    expires: datetime
    activation: str | None = None
    query: str | None = None


def catalog():
    from operations import fibonetics_read_report as fibo, recent_import_read_report as recent
    from operations import exact_login_probe as exact, paragon_login_probe as paragon
    tasks=[Task(fibo,fibo.REPORT,fibo.EXPIRES),Task(recent,recent.REPORT,recent.EXPIRES),
        Task(exact,exact.PROBE_ID,exact.EXPIRES_AT,'EXACT_LOGIN_PROBE_ID',
            "SELECT result->>'status' FROM exact_login_probes WHERE probe_id=%s"),
        Task(paragon,paragon.PROBE_ID,paragon.EXPIRES_AT,'PARAGON_LOGIN_PROBE_ID',
            "SELECT result->>'status' FROM paragon_login_probes WHERE probe_id=%s")]
    return {task.task_id:task for task in tasks}


def validate(job):
    task=catalog().get(job['task_key'])
    if task is None or job['action']!='run' or job['params']:
        raise ValueError('unknown_readonly_command')
    if datetime.now(timezone.utc)>=task.expires:
        raise ValueError('readonly_historical_authorization_expired')
    return task


def probe_completed(app,task):
    with app._db_connect() as conn:
        row=conn.execute(task.query,(task.task_id,)).fetchone()
    if not row or row[0]!='passed':
        raise ValueError('probe_has_no_confirmed_completion')


async def dispatch(app,job):
    if job['action']=='daily_report':
        from operations.nightly_reports import run
        return await run(app,job)
    task=validate(job)
    if task.query:
        await task.module.run(task_id=task.task_id)
        await asyncio.to_thread(probe_completed,app,task)
    elif await task.module.run(app) is not True:
        # A historical claim alone is not proof of completed output.
        raise ValueError('report_not_completed_or_already_claimed')


def seed_configured(conn,app):
    jobs.initialize(conn)
    for task in catalog().values():
        if datetime.now(timezone.utc)>=task.expires:continue
        if task.activation and os.environ.get(task.activation)!=task.task_id:continue
        try:
            result=jobs.submit(conn,app.DIVISION,'reports',task.task_id,'run',{},
                'configured:'+task.task_id,task.expires)
            if result['state'] in {'queued','running','uncertain'}:break
        except jobs.JobConflict:break


async def serve(app):
    async def run(job):await dispatch(app,job)
    async def seed():await asyncio.to_thread(jobs.database_call,app,seed_configured,app)
    await jobs.consume(app,'reports',run,seed=seed)
