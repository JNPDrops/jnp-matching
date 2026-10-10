"""Calendar-safe batch identities. Registration alone never claims execution.

Uses the existing database and records every due slot after an outage. Source
adapters must accept immutable cutoff/revision identities before consuming these
plans; historical daily imports must not be relabelled as intraday imports.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE=ZoneInfo('Europe/Amsterdam')
DIVISION=3977752
HOURS=(1,9,13,17,21)


@dataclass(frozen=True)
class Batch:
    planned_at: datetime
    processing_date: date
    cutoff: datetime
    final: bool

    @property
    def key(self):
        return f'jnp:{DIVISION}:batch:{self.planned_at.astimezone(ZONE):%Y-%m-%dT%H%M}'

    @property
    def start(self):
        return datetime.combine(self.processing_date,time.min,ZONE).astimezone(timezone.utc)


def batch_at(planned):
    if planned.tzinfo is None:
        raise ValueError('planned_time_requires_timezone')
    local=planned.astimezone(ZONE)
    if local.hour not in HOURS or local.minute or local.second or local.microsecond:
        raise ValueError('not_a_processing_slot')
    final=local.hour==1
    day=local.date()-timedelta(days=1) if final else local.date()
    cutoff=datetime.combine(local.date(),time.min if final else time(local.hour-1),ZONE)
    return Batch(local,day,cutoff.astimezone(timezone.utc),final)


def due_batches(first_planned_date, now):
    if type(first_planned_date) is not date or now.tzinfo is None:
        raise ValueError('invalid_schedule_bounds')
    last=now.astimezone(ZONE).date()
    day=first_planned_date
    while day<=last:
        for hour in HOURS:
            planned=datetime.combine(day,time(hour),ZONE)
            if planned<=now:
                yield batch_at(planned)
        day+=timedelta(days=1)


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_batches (
        batch_key text PRIMARY KEY,division integer NOT NULL,
        planned_at timestamptz NOT NULL,processing_date date NOT NULL,
        window_start timestamptz NOT NULL,window_end timestamptz NOT NULL,
        final boolean NOT NULL,state text NOT NULL DEFAULT 'pending',
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(division,planned_at),CHECK(window_start<window_end))''')


def register_due(conn, first_planned_date, now):
    """Atomic idempotent registration; never resets an existing batch."""
    with conn.transaction():
        initialize(conn)
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('jnp:processing-schedule',0))")
        for b in due_batches(first_planned_date,now):
            conn.execute('''INSERT INTO jnp_processing_batches
                (batch_key,division,planned_at,processing_date,window_start,window_end,final)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (b.key,DIVISION,b.planned_at,b.processing_date,b.start,b.cutoff,b.final))
            row=conn.execute('''SELECT planned_at,processing_date,window_start,window_end,final
                FROM jnp_processing_batches WHERE batch_key=%s''',(b.key,)).fetchone()
            if row!=(b.planned_at,b.processing_date,b.start,b.cutoff,b.final):
                raise ValueError('persisted_batch_window_conflict')
