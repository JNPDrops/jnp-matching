"""Durable, single-chain coordinator hosted by the existing reports worker.

Registration and dispatch are separate from completion. A queue's `completed`
flag is never financial evidence: each adapter must commit its own outcome.
No interrupted or uncertain command is reset or automatically resubmitted.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import os

from operations import agent_jobs as jobs, processing_schedule as schedule
from operations import processing_alerts as alerts

STAGES = (
    ('fibonatix_source', 'fibonatix'), ('fibonatix_import', 'fibonatix'),
    ('icepay_source', 'icepay'), ('icepay_import', 'icepay'),
    ('routing', 'reports'), ('woo_rules', 'reports'), ('tax', 'reports'),
    ('maintenance', 'reports'), ('fibonatix_automatically', 'fibonatix'),
    ('icepay_automatically', 'icepay'), ('report', 'reports'),
)
DEPENDENCIES = {
    'fibonatix_import': ('fibonatix_source',),
    'icepay_import': ('icepay_source',),
    'fibonatix_automatically': ('fibonatix_import', 'routing', 'woo_rules', 'tax', 'maintenance'),
    'icepay_automatically': ('icepay_import', 'routing', 'woo_rules', 'tax', 'maintenance'),
}
TERMINAL = frozenset({'verified', 'blocked', 'uncertain'})


def _initialize(conn):
    schedule.initialize(conn)
    alerts.initialize(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_control (
        division integer PRIMARY KEY,enabled boolean NOT NULL DEFAULT false,
        first_planned_date date NOT NULL,heartbeat_at timestamptz,
        code_commit text,updated_at timestamptz NOT NULL DEFAULT now())''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_stages (
        batch_key text NOT NULL REFERENCES jnp_processing_batches(batch_key),
        stage text NOT NULL,state text NOT NULL DEFAULT 'pending',
        job_id uuid,evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(batch_key,stage),
        CHECK(state IN ('pending','queued','verified','blocked','uncertain')))''')


def initialize(conn):
    # Reports and the independent watchdog may start simultaneously.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('jnp:processing-schema',0))")
        _initialize(conn)


def choose_stage(states):
    """Independent later work proceeds after a block; reports are always last."""
    for stage, role in STAGES:
        state = states.get(stage, 'pending')
        if state in TERMINAL:
            continue
        if state != 'pending':
            return None
        failed = [d for d in DEPENDENCIES.get(stage, ()) if states.get(d) != 'verified']
        return stage, role, failed
    return None


def outcome(conn, batch, stage, state, evidence, job_id=None):
    if state not in TERMINAL:
        raise ValueError('invalid_processing_outcome')
    with conn.transaction():
        row = conn.execute('''SELECT state,job_id,evidence FROM jnp_processing_stages
            WHERE batch_key=%s AND stage=%s FOR UPDATE''', (batch,stage)).fetchone()
        if not row:
            raise ValueError('unregistered_processing_stage')
        if row[0] in TERMINAL:
            if row[0] == state and row[2] == evidence:
                return
            raise ValueError('terminal_processing_outcome_is_immutable')
        if job_id is not None and str(row[1]) != str(job_id):
            raise ValueError('processing_job_identity_changed')
        conn.execute('''UPDATE jnp_processing_stages SET state=%s,evidence=%s::jsonb,
            updated_at=now() WHERE batch_key=%s AND stage=%s''',
            (state,json.dumps(evidence),batch,stage))
        if state != 'verified':
            alerts.enqueue(conn,batch,stage,evidence.get('reason','stage_not_verified'))


def tick(conn, app, now=None):
    """Short DB transaction only: provider work happens in the assigned workers."""
    initialize(conn)
    now = now or datetime.now(timezone.utc)
    from operations.worker_write_fence import current_owner
    owner = current_owner()
    if owner is None or owner.role != "reports" or owner.division != app.DIVISION:
        raise ValueError("assigned_reports_coordinator_required")
    with conn.transaction():
        if not conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('jnp:processing-coordinator',0))").fetchone()[0]:
            return 'busy'
        if not conn.execute('''SELECT 1 FROM jnp_worker_roles WHERE division=%s AND role='reports'
            AND lease_id=%s AND active_owner=%s AND NOT draining AND lease_until>clock_timestamp()''',
            (app.DIVISION,owner.lease_id,owner.owner)).fetchone():
            return 'lease_unavailable'
        control = conn.execute('''SELECT enabled,first_planned_date
            FROM jnp_processing_control WHERE division=%s FOR UPDATE''', (app.DIVISION,)).fetchone()
        if not control or not control[0]:
            return 'disabled'
        schedule.register_due(conn,control[1],now)
        conn.execute('''UPDATE jnp_processing_control SET heartbeat_at=%s,code_commit=%s
            WHERE division=%s''', (now,os.environ.get('RENDER_GIT_COMMIT','unknown'),app.DIVISION))
        batch = conn.execute('''SELECT batch_key FROM jnp_processing_batches
            WHERE division=%s AND state IN ('pending','running')
            ORDER BY planned_at LIMIT 1 FOR UPDATE''', (app.DIVISION,)).fetchone()
        if not batch:
            return 'idle'
        key = batch[0]
        # Finish independent work and the incomplete report in the active chain.
        # Only a later chain is held behind an unresolved earlier write.
        if conn.execute('''SELECT 1 FROM jnp_processing_stages s
            JOIN jnp_processing_batches b ON b.batch_key=s.batch_key
            WHERE b.division=%s AND s.state='uncertain' AND b.batch_key<>%s
              AND b.planned_at<(SELECT planned_at FROM jnp_processing_batches WHERE batch_key=%s)
            LIMIT 1''',(app.DIVISION,key,key)).fetchone():
            return 'uncertain_previous_chain_requires_review'
        conn.execute("UPDATE jnp_processing_batches SET state='running' WHERE batch_key=%s",(key,))
        for stage,_ in STAGES:
            conn.execute('''INSERT INTO jnp_processing_stages(batch_key,stage)
                VALUES(%s,%s) ON CONFLICT DO NOTHING''',(key,stage))
        rows = conn.execute('''SELECT s.stage,s.state,s.job_id,j.state
            FROM jnp_processing_stages s LEFT JOIN jnp_agent_jobs j ON j.job_id=s.job_id
            WHERE s.batch_key=%s''',(key,)).fetchall()
        if any(job_state in {'queued','running'} for _,_,_,job_state in rows):
            return 'worker_still_running'
        for stage,state,job_id,job_state in rows:
            if state == 'queued' and job_state in {'blocked','uncertain','completed'}:
                # Completed without committed adapter proof is a failure, not success.
                outcome(conn,key,stage,'uncertain' if job_state=='uncertain' else 'blocked',
                        {'reason':'adapter_completion_evidence_missing' if job_state=='completed' else 'worker_command_'+job_state},job_id)
        states = dict(conn.execute('SELECT stage,state FROM jnp_processing_stages WHERE batch_key=%s',(key,)).fetchall())
        next_stage = choose_stage(states)
        if next_stage is None:
            if all(s in TERMINAL for s in states.values()):
                state = 'verified' if all(s=='verified' for s in states.values()) else 'incomplete'
                conn.execute('UPDATE jnp_processing_batches SET state=%s WHERE batch_key=%s',(state,key))
            return 'waiting'
        stage,role,failed = next_stage
        if failed:
            outcome(conn,key,stage,'blocked',{'reason':'dependency_not_verified','dependencies':failed})
            return 'dependency_blocked'
        try:
            command = jobs.submit(conn,app.DIVISION,role,key,'processing_stage',
                                  {'stage':stage},key+':'+stage)
        except jobs.JobConflict:
            if conn.execute('''SELECT 1 FROM jnp_agent_jobs WHERE division=%s AND role=%s
                AND state='uncertain' LIMIT 1''',(app.DIVISION,role)).fetchone():
                outcome(conn,key,stage,'blocked',{'reason':'assigned_role_has_unresolved_command'})
                return 'unresolved_role_blocked'
            return 'worker_busy'
        conn.execute("UPDATE jnp_processing_stages SET state='queued',job_id=%s,updated_at=now() WHERE batch_key=%s AND stage=%s",
                     (command['job_id'],key,stage))
        return 'queued'


def watchdog(conn, app, now=None):
    """Runs on a different existing worker, including while the coordinator is down."""
    initialize(conn)
    now = now or datetime.now(timezone.utc)
    control = conn.execute('SELECT enabled,first_planned_date,heartbeat_at FROM jnp_processing_control WHERE division=%s',(app.DIVISION,)).fetchone()
    if not control or not control[0]:
        return
    schedule.register_due(conn,control[1],now)
    for key,planned,state in conn.execute("SELECT batch_key,planned_at,state FROM jnp_processing_batches WHERE division=%s AND state IN ('pending','running')",(app.DIVISION,)).fetchall():
        if now-planned > timedelta(minutes=30) and state=='pending':
            alerts.enqueue(conn,key,'coordinator','scheduled_run_not_started')
        if now-planned > timedelta(hours=3):
            alerts.enqueue(conn,key,'coordinator','scheduled_run_overdue')
        if (control[2] is None or now-control[2]>timedelta(minutes=5)) and now-planned>timedelta(minutes=5):
            alerts.enqueue(conn,key,'coordinator','coordinator_heartbeat_missing')


async def pulse(app, *, monitor=False):
    await asyncio.to_thread(jobs.database_call,app,watchdog if monitor else tick,app)
    if monitor:
        try:
            await asyncio.to_thread(alerts.deliver_one,app,os.environ)
        except Exception:
            # Outbox remains pending and watchdog stays alive. No provider text in logs.
            import logging
            logging.getLogger('uvicorn.error').warning('processing_notification_delivery_unavailable')


async def monitor_loop(app):
    """Independent of the coordinator and long provider jobs, on ICEPAY's owner."""
    from operations import task_drain
    import logging
    while not task_drain.requested():
        try:
            await pulse(app,monitor=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger('uvicorn.error').warning('processing_watchdog_unavailable')
        await task_drain.wait(60)


async def coordinator_loop(app):
    from operations import task_drain
    import logging
    while not task_drain.requested():
        try:
            await pulse(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger('uvicorn.error').warning('processing_coordinator_unavailable')
        await task_drain.wait(5)
