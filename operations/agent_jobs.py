"""Durable bounded command queue. No arbitrary code, URLs, credentials or retries.

Role-specific adapters validate commands before submission and again before
execution. An abandoned running job blocks the role until explicit review.
"""
import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

from operations import worker_coordination as c, worker_write_fence as fence, task_drain

ROLES = {'fibonatix', 'icepay', 'reports'}


class JobConflict(c.CoordinationError):
    pass


def initialize(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('jnp:agent-jobs-schema',0))")
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_agent_jobs (
            job_id UUID PRIMARY KEY, division BIGINT NOT NULL, role TEXT NOT NULL,
            task_key TEXT NOT NULL, action TEXT NOT NULL, request_key TEXT NOT NULL,
            params JSONB NOT NULL, expires_at TIMESTAMPTZ,
            state TEXT NOT NULL DEFAULT 'queued', claim_lease UUID, error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(division,role,request_key),
            CHECK(state IN ('queued','running','completed','blocked','uncertain')),
            CHECK(role IN ('fibonatix','icepay','reports')))''')


def submit(conn, division, role, task_key, action, params, request_key, expires_at=None):
    if role not in ROLES or not all(isinstance(x,str) and 0<len(x)<=160 for x in (task_key,action,request_key)):
        raise ValueError('invalid_job_identity')
    if not isinstance(params,dict) or len(json.dumps(params))>2048:
        raise ValueError('invalid_job_params')
    c.validate_identity(division,'main',role)
    with conn.transaction():
        c.lock(conn,division,'role:'+role)
        row=conn.execute('''SELECT job_id,task_key,action,params,state FROM jnp_agent_jobs
            WHERE division=%s AND role=%s AND request_key=%s''',(division,role,request_key)).fetchone()
        if row:
            if (row[1],row[2],row[3])!=(task_key,action,params):
                raise JobConflict('idempotency_key_has_different_command')
            return {'job_id':str(row[0]),'state':row[4],'duplicate':True}
        if conn.execute('''SELECT 1 FROM jnp_agent_jobs WHERE division=%s AND role=%s
            AND state IN ('queued','running','uncertain') LIMIT 1''',(division,role)).fetchone():
            raise JobConflict('role_has_pending_or_unresolved_job')
        if expires_at and expires_at <= datetime.now(timezone.utc):
            raise JobConflict('command_authorization_expired')
        job=str(uuid4())
        conn.execute('''INSERT INTO jnp_agent_jobs
            (job_id,division,role,task_key,action,params,request_key,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)''',
            (job,division,role,task_key,action,json.dumps(params),request_key,expires_at))
        return {'job_id':job,'state':'queued','duplicate':False}


def claim_next(conn, lease):
    with conn.transaction():
        c.lock(conn,lease.division,'role:'+lease.role)
        row=conn.execute('''SELECT active_owner,lease_id,draining,lease_until>clock_timestamp()
            FROM jnp_worker_roles WHERE division=%s AND role=%s FOR UPDATE''',
            (lease.division,lease.role)).fetchone()
        if not row or row[0]!=lease.owner or str(row[1])!=str(lease.lease_id) or row[2] or not row[3]:
            raise c.LeaseUnavailable('role_not_available_for_job')
        # No automatic replay after a lost process, including read-only jobs.
        abandoned=conn.execute('''UPDATE jnp_agent_jobs SET state='uncertain',
            error_code='prior_process_ended_review_required',updated_at=NOW()
            WHERE division=%s AND role=%s AND state='running' AND claim_lease<>%s
            RETURNING job_id''',(lease.division,lease.role,lease.lease_id)).fetchall()
        if abandoned or conn.execute('''SELECT 1 FROM jnp_agent_jobs
            WHERE division=%s AND role=%s AND state IN ('running','uncertain') LIMIT 1''',
            (lease.division,lease.role)).fetchone():
            return None
        if conn.execute('''SELECT 1 FROM jnp_worker_writes WHERE division=%s
            AND role=%s AND state='unresolved' LIMIT 1''',(lease.division,lease.role)).fetchone():
            return None
        conn.execute('''UPDATE jnp_agent_jobs SET state='blocked',error_code='authorization_expired',updated_at=NOW()
            WHERE division=%s AND role=%s AND state='queued' AND expires_at<=clock_timestamp()''',
            (lease.division,lease.role))
        row=conn.execute('''SELECT job_id,task_key,action,params FROM jnp_agent_jobs
            WHERE division=%s AND role=%s AND state='queued'
            ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1''',
            (lease.division,lease.role)).fetchone()
        if not row:return None
        conn.execute('''UPDATE jnp_agent_jobs SET state='running',claim_lease=%s,updated_at=NOW()
            WHERE job_id=%s''',(lease.lease_id,row[0]))
        return {'job_id':str(row[0]),'task_key':row[1],'action':row[2],'params':row[3]}


def finish(conn, job, lease, state, error=None):
    if state not in {'completed','blocked','uncertain'}:raise ValueError('invalid_job_outcome')
    row=conn.execute('''UPDATE jnp_agent_jobs SET state=%s,error_code=%s,updated_at=NOW()
        WHERE job_id=%s AND claim_lease=%s AND state='running' RETURNING job_id''',
        (state,error,job['job_id'],lease.lease_id)).fetchone()
    if not row:raise JobConflict('job_completion_owner_changed')


def snapshot(conn, division, role):
    rows=conn.execute('''SELECT job_id,task_key,action,state,error_code,updated_at
        FROM jnp_agent_jobs WHERE division=%s AND role=%s
        ORDER BY created_at DESC LIMIT 20''',(division,role)).fetchall()
    return [{'job_id':str(r[0]),'task_key':r[1],'action':r[2],'state':r[3],
             'error_code':r[4],'updated_at':r[5].isoformat()} for r in rows]


def database_call(app, function, *args):
    with app._db_connect() as conn:return function(conn,*args)


async def execute(app, lease, job, dispatch):
    state,error='completed',None
    try:
        with fence.job_scope(job['job_id']):
            await dispatch(job)
        if lease.lost.is_set():state,error='uncertain','write_requires_review'
    except asyncio.CancelledError:
        await asyncio.to_thread(database_call,app,finish,job,lease,'uncertain','worker_interrupted')
        raise
    except Exception as exc:
        state='uncertain' if lease.lost.is_set() else 'blocked'
        error=type(exc).__name__  # no provider response, token or financial data
    await asyncio.to_thread(database_call,app,finish,job,lease,state,error)


async def consume(app, role, dispatch):
    lease=fence.current_owner()
    if lease is None or lease.role!=role:
        # Local development without a database must never execute queued work.
        while not task_drain.requested():await task_drain.wait(5)
        return
    await asyncio.to_thread(database_call,app,initialize)
    while not task_drain.requested() and not lease.lost.is_set():
        try:
            job=await asyncio.to_thread(database_call,app,claim_next,lease)
        except c.LeaseUnavailable:
            return
        if job:
            # If handover races claim, finish the claimed command; every write
            # still checks the live lease. A pending stop admits no next job.
            await execute(app,lease,job,dispatch)
        else:
            await task_drain.wait(5)
