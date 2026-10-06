"""Immutable day identities and fail-closed gates for authorized nightly work.

This module does not post financial transactions or schedule workers. It records
the calendar day from the planned local start, not from a later retry's clock.
Only verified stage outcomes can release the final report.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import json

ZONE = ZoneInfo('Europe/Amsterdam')
DIVISION = 3977752
STAGES = ('fibonatix', 'icepay', 'routing', 'woo-rules', 'tax',
          'maintenance', 'automatically', 'reports')
TERMINAL = {'completed', 'blocked'}


def window(planned_start):
    if not isinstance(planned_start, datetime) or planned_start.tzinfo is None:
        raise ValueError('planned_start_requires_timezone')
    day = planned_start.astimezone(ZONE).date() - timedelta(days=1)
    lo = datetime.combine(day, time.min, ZONE)
    hi = datetime.combine(day + timedelta(days=1), time.min, ZONE)
    return day, lo.astimezone(timezone.utc), hi.astimezone(timezone.utc)


def identity(day, psp):
    if type(day) is not date or psp not in {'fibonatix', 'icepay'}:
        raise ValueError('invalid_daily_source_identity')
    return f'jnp:{DIVISION}:{psp}:{day.isoformat()}'


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_nightly_runs (
        division integer NOT NULL, processing_date date NOT NULL,
        window_start timestamptz NOT NULL, window_end timestamptz NOT NULL,
        state text NOT NULL, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(division,processing_date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_nightly_stages (
        division integer NOT NULL, processing_date date NOT NULL,
        stage text NOT NULL, state text NOT NULL, evidence jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(division,processing_date,stage),
        FOREIGN KEY(division,processing_date)
          REFERENCES jnp_nightly_runs(division,processing_date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_daily_source_jobs (
        division integer NOT NULL, psp text NOT NULL,
        processing_date date NOT NULL, job_key text NOT NULL UNIQUE,
        source_state text NOT NULL DEFAULT 'pending',
        import_state text NOT NULL DEFAULT 'pending',
        evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(division,psp,processing_date),
        CHECK(psp IN ('fibonatix','icepay')),
        FOREIGN KEY(division,processing_date)
          REFERENCES jnp_nightly_runs(division,processing_date))''')


def register(conn, planned_start):
    day, lo, hi = window(planned_start)
    with conn.transaction():
        initialize(conn)
        conn.execute('''INSERT INTO jnp_nightly_runs
            (division,processing_date,window_start,window_end,state,detail)
            VALUES(%s,%s,%s,%s,'preflight',%s::jsonb) ON CONFLICT DO NOTHING''',
            (DIVISION,day,lo,hi,json.dumps({'matching':'exact_automatically',
             'planned_start':planned_start.isoformat()})))
        row = conn.execute('''SELECT window_start,window_end FROM jnp_nightly_runs
            WHERE division=%s AND processing_date=%s FOR UPDATE''',
            (DIVISION,day)).fetchone()
        if not row or tuple(row) != (lo, hi):
            raise ValueError('persisted_day_window_conflict')
        for psp in ('fibonatix','icepay'):
            conn.execute('''INSERT INTO jnp_daily_source_jobs
                (division,psp,processing_date,job_key) VALUES(%s,%s,%s,%s)
                ON CONFLICT DO NOTHING''',(DIVISION,psp,day,identity(day,psp)))
    return day


def lock_chain(conn):
    """Keep this dedicated connection open for the complete chain.

    Uncertain stage/import state still requires explicit evidence-based review
    after a lost session; acquiring this lock never resets any work.
    """
    if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended('jnp:3977752:nightly-chain',0))").fetchone()[0]:
        raise ValueError('another_nightly_chain_is_active')


def unlock_chain(conn):
    conn.execute("SELECT pg_advisory_unlock(hashtextextended('jnp:3977752:nightly-chain',0))")


def report_gate(states):
    before = STAGES[:-1]
    if any(states.get(s) not in TERMINAL for s in before):
        raise ValueError('day_work_not_terminal')
    return 'incomplete' if any(states[s] == 'blocked' for s in before) else 'completed'


def record(conn, day, stage, state, evidence):
    if stage not in STAGES or state not in {'pending','running','completed','blocked','uncertain'}:
        raise ValueError('invalid_stage_state')
    if not isinstance(evidence,dict) or not evidence:
        raise ValueError('stage_requires_evidence')
    if state == 'completed' and not evidence.get('verified'):
        raise ValueError('completion_requires_verified_evidence')
    with conn.transaction():
        run = conn.execute('''SELECT processing_date FROM jnp_nightly_runs
            WHERE division=%s AND processing_date=%s FOR UPDATE''',
            (DIVISION,day)).fetchone()
        if not run:
            raise ValueError('day_not_registered')
        prior = conn.execute('''SELECT state,evidence FROM jnp_nightly_stages
            WHERE division=%s AND processing_date=%s AND stage=%s''',
            (DIVISION,day,stage)).fetchone()
        if prior and prior[0] in {'completed','uncertain'}:
            if (prior[0],prior[1]) == (state,evidence):
                return
            raise ValueError('completed_or_uncertain_stage_is_immutable')
        if stage == 'reports' and state in {'running','completed'}:
            states = dict(conn.execute('''SELECT stage,state FROM jnp_nightly_stages
                WHERE division=%s AND processing_date=%s''',(DIVISION,day)).fetchall())
            report_gate(states)
        conn.execute('''INSERT INTO jnp_nightly_stages
            (division,processing_date,stage,state,evidence) VALUES(%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(division,processing_date,stage) DO UPDATE
            SET state=EXCLUDED.state,evidence=EXCLUDED.evidence,updated_at=now()''',
            (DIVISION,day,stage,state,json.dumps(evidence)))


def missing_receipts(rows, imported):
    """Exclude only proven identical PSP IDs; conflicts block, never overwrite."""
    seen = set()
    missing = []
    for row in rows:
        key = str(row['payment_id'])
        if not key or key in seen:
            raise ValueError('duplicate_source_payment_id')
        seen.add(key)
        if key in imported:
            if imported[key] != row:
                raise ValueError('imported_payment_evidence_conflict')
        else:
            missing.append(row)
    return missing
