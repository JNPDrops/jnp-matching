"""Durable coordination primitives for split workers.

This module stores only operational metadata. It never stores credentials,
request URLs, payloads, customer records, or provider response bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
import os
import re
import socket
from uuid import UUID, uuid4


ROLES = frozenset({"routing", "woo-rules", "tax", "maintenance", "fibonatix", "icepay", "reports"})
CONNECTIONS = frozenset({"main", "allocation"})
METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
PRIORITIES = frozenset({"critical", "routine", "bulk"})
UNKNOWN_PER_MINUTE = 30
MAX_LEASE_SECONDS = 300


class CoordinationError(RuntimeError):
    pass


class BudgetDeferred(CoordinationError):
    pass


class LeaseUnavailable(CoordinationError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def validate_identity(division, connection, role):
    if type(division) is not int or division <= 0:
        raise ValueError("invalid_division")
    if connection not in CONNECTIONS or role not in ROLES:
        raise ValueError("invalid_worker_identity")


def parse_headers(headers):
    """Return only non-negative numeric quota metadata."""
    result = {}
    for name, header in (
        ("daily_limit", "x-ratelimit-limit"),
        ("daily_remaining", "x-ratelimit-remaining"),
        ("daily_reset_ms", "x-ratelimit-reset"),
        ("minute_limit", "x-ratelimit-minutely-limit"),
        ("minute_remaining", "x-ratelimit-minutely-remaining"),
        ("minute_reset_ms", "x-ratelimit-minutely-reset"),
    ):
        value = str(headers.get(header, ""))
        if value.isdigit():
            result[name] = int(value)
    return result


def available(state, pending, *, floor, now_ms, unknown_count=0):
    """Pure conservative decision used under the database lock."""
    if type(floor) is not int or floor < 0:
        raise ValueError("invalid_budget_floor")
    daily = state.get("daily_remaining")
    daily_reset = state.get("daily_reset_ms")
    if type(daily_reset) is int and now_ms >= daily_reset:
        daily = None
    minute = state.get("minute_remaining")
    minute_reset = state.get("minute_reset_ms")
    if type(minute_reset) is int and now_ms >= minute_reset:
        minute = None
    if type(daily) is int and daily - pending <= floor:
        return False, "daily_reserve"
    if type(minute) is int and minute - pending <= 0:
        return False, "minute_reserve"
    if minute is None and unknown_count >= UNKNOWN_PER_MINUTE:
        return False, "unknown_minute_cap"
    return True, None


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_worker_roles (
        division INTEGER NOT NULL, role TEXT NOT NULL,
        desired_owner TEXT, active_owner TEXT, lease_id UUID,
        lease_until TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ,
        draining BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'inactive', detail JSONB NOT NULL DEFAULT '{}',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(division,role),
        CHECK(role IN ('routing','woo-rules','tax','maintenance','fibonatix','icepay','reports')),
        CHECK(status IN ('inactive','starting','ready','waiting','draining','stopped','error'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_exact_api_budget (
        division INTEGER NOT NULL, connection TEXT NOT NULL,
        daily_limit INTEGER, daily_remaining INTEGER, daily_reset_ms BIGINT,
        minute_limit INTEGER, minute_remaining INTEGER, minute_reset_ms BIGINT,
        observed_at TIMESTAMPTZ,
        unknown_window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        unknown_window_count INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(division,connection),
        CHECK(connection IN ('main','allocation'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_exact_api_reservations (
        request_id UUID PRIMARY KEY, division INTEGER NOT NULL, connection TEXT NOT NULL,
        role TEXT NOT NULL, priority TEXT NOT NULL, method TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'reserved', reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        CHECK(connection IN ('main','allocation')),
        CHECK(role IN ('routing','woo-rules','tax','maintenance','fibonatix','icepay','reports')),
        CHECK(priority IN ('critical','routine','bulk')),
        CHECK(method IN ('GET','POST','PUT','DELETE')),
        CHECK(state IN ('reserved','observed','uncertain','released'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS jnp_exact_api_reservations_active ON jnp_exact_api_reservations(division,connection,reserved_at) WHERE state IN ('reserved','uncertain')")


def lock(conn, division, scope):
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"jnp:{division}:{scope}",))


def claim_role(conn, division, role, owner, *, lease_seconds=90, now=None):
    validate_identity(division, "main", role)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{2,119}", owner or ""):
        raise ValueError("invalid_owner")
    if type(lease_seconds) is not int or not 15 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError("invalid_lease")
    now = now or utcnow()
    lease_id = str(uuid4())
    with conn.transaction():
        lock(conn, division, "role:" + role)
        conn.execute("INSERT INTO jnp_worker_roles(division,role) VALUES(%s,%s) ON CONFLICT DO NOTHING", (division, role))
        row = conn.execute("SELECT desired_owner,active_owner,lease_until,draining FROM jnp_worker_roles WHERE division=%s AND role=%s FOR UPDATE", (division, role)).fetchone()
        desired, active, until, draining = row
        handover = draining and active is None and desired == owner
        if (draining and not handover) or (desired is not None and desired != owner):
            raise LeaseUnavailable("role_not_assigned")
        if active not in (None, owner) and until is not None and until > now:
            raise LeaseUnavailable("role_already_owned")
        conn.execute("""UPDATE jnp_worker_roles SET active_owner=%s,lease_id=%s,
            lease_until=%s,heartbeat_at=%s,draining=FALSE,status='starting',detail='{}',updated_at=%s
            WHERE division=%s AND role=%s""",
            (owner, lease_id, now + timedelta(seconds=lease_seconds), now, now, division, role))
    return lease_id


def heartbeat(conn, division, role, owner, lease_id, status, detail=None, *, lease_seconds=90, now=None):
    validate_identity(division, "main", role)
    UUID(str(lease_id))
    if status not in {"starting", "ready", "waiting", "draining", "error"}:
        raise ValueError("invalid_worker_status")
    if type(lease_seconds) is not int or not 15 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError("invalid_lease")
    safe = {key: value for key, value in (detail or {}).items()
            if key in {"phase", "reason", "task", "last_success_at", "queue_depth"}
            and isinstance(value, (str, int, float, bool, type(None)))}
    now = now or utcnow()
    row = conn.execute("""UPDATE jnp_worker_roles SET heartbeat_at=%s,lease_until=%s,
        status=%s,detail=%s::jsonb,updated_at=%s
        WHERE division=%s AND role=%s AND active_owner=%s AND lease_id=%s AND lease_until>%s
        RETURNING draining""",
        (now, now + timedelta(seconds=lease_seconds), status, json.dumps(safe), now,
         division, role, owner, str(lease_id), now)).fetchone()
    if row is None:
        raise LeaseUnavailable("role_lease_lost")
    return {"draining": bool(row[0]), "lease_until": now + timedelta(seconds=lease_seconds)}


def begin_drain(conn, division, role, *, desired_owner=None):
    validate_identity(division, "main", role)
    if desired_owner is not None and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{2,119}", desired_owner):
        raise ValueError("invalid_owner")
    with conn.transaction():
        lock(conn, division, "role:" + role)
        conn.execute("INSERT INTO jnp_worker_roles(division,role) VALUES(%s,%s) ON CONFLICT DO NOTHING", (division, role))
        conn.execute("""UPDATE jnp_worker_roles SET draining=TRUE,desired_owner=%s,
            status=CASE WHEN active_owner IS NULL THEN 'stopped' ELSE 'draining' END,
            updated_at=NOW() WHERE division=%s AND role=%s""", (desired_owner, division, role))


def release_role(conn, division, role, owner, lease_id, *, status="stopped"):
    validate_identity(division, "main", role)
    UUID(str(lease_id))
    if status not in {"stopped", "error"}:
        raise ValueError("invalid_release_status")
    row = conn.execute("""UPDATE jnp_worker_roles SET active_owner=NULL,lease_id=NULL,
        lease_until=NULL,heartbeat_at=NOW(),status=%s,updated_at=NOW()
        WHERE division=%s AND role=%s AND active_owner=%s AND lease_id=%s RETURNING role""",
        (status, division, role, owner, str(lease_id))).fetchone()
    if row is None:
        raise LeaseUnavailable("role_lease_lost")


@dataclass(frozen=True)
class Reservation:
    request_id: str
    division: int
    connection: str
    role: str
    method: str


def reserve_request(conn, division, connection, role, method, *, priority="routine", floor=200, now=None):
    validate_identity(division, connection, role)
    method = str(method).upper()
    if method not in METHODS or priority not in PRIORITIES:
        raise ValueError("invalid_reservation")
    now = now or utcnow()
    now_ms = int(now.timestamp() * 1000)
    request_id = str(uuid4())
    with conn.transaction():
        lock(conn, division, "budget:" + connection)
        conn.execute("INSERT INTO jnp_exact_api_budget(division,connection) VALUES(%s,%s) ON CONFLICT DO NOTHING", (division, connection))
        row = conn.execute("""SELECT daily_limit,daily_remaining,daily_reset_ms,
            minute_limit,minute_remaining,minute_reset_ms,observed_at,
            unknown_window_started_at,unknown_window_count
            FROM jnp_exact_api_budget WHERE division=%s AND connection=%s FOR UPDATE""",
            (division, connection)).fetchone()
        state = dict(zip(("daily_limit", "daily_remaining", "daily_reset_ms", "minute_limit",
                          "minute_remaining", "minute_reset_ms", "observed_at",
                          "unknown_window_started_at", "unknown_window_count"), row))
        window_start, unknown_count = state["unknown_window_started_at"], state["unknown_window_count"]
        if now - window_start >= timedelta(minutes=1):
            window_start, unknown_count = now, 0
        pending = conn.execute("""SELECT COUNT(*) FROM jnp_exact_api_reservations
            WHERE division=%s AND connection=%s AND state IN ('reserved','uncertain')
              AND reserved_at>COALESCE(%s,'epoch'::timestamptz)""",
            (division, connection, state["observed_at"])).fetchone()[0]
        allowed, reason = available(state, pending, floor=floor, now_ms=now_ms, unknown_count=unknown_count)
        if not allowed:
            raise BudgetDeferred(reason)
        conn.execute("""INSERT INTO jnp_exact_api_reservations
            (request_id,division,connection,role,priority,method,reserved_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (request_id, division, connection, role, priority, method, now))
        conn.execute("""UPDATE jnp_exact_api_budget SET unknown_window_started_at=%s,
            unknown_window_count=%s,updated_at=%s WHERE division=%s AND connection=%s""",
            (window_start, unknown_count + 1, now, division, connection))
    return Reservation(request_id, division, connection, role, method)


def complete_request(conn, reservation, headers, *, outcome="response", now=None):
    if outcome not in {"response", "not_sent", "uncertain"}:
        raise ValueError("invalid_outcome")
    now = now or utcnow()
    values = {} if outcome == "not_sent" else parse_headers(headers)
    with conn.transaction():
        lock(conn, reservation.division, "budget:" + reservation.connection)
        row = conn.execute("SELECT state FROM jnp_exact_api_reservations WHERE request_id=%s FOR UPDATE", (reservation.request_id,)).fetchone()
        if row is None or row[0] != "reserved":
            raise CoordinationError("reservation_not_active")
        state = "released" if outcome == "not_sent" else ("observed" if values else "uncertain")
        conn.execute("UPDATE jnp_exact_api_reservations SET state=%s,completed_at=%s WHERE request_id=%s", (state, now, reservation.request_id))
        if values:
            assignments = ",".join(key + "=%s" for key in values)
            conn.execute(f"UPDATE jnp_exact_api_budget SET {assignments},observed_at=%s,updated_at=%s WHERE division=%s AND connection=%s",
                         (*values.values(), now, now, reservation.division, reservation.connection))
    return {"state": state, "observed": values}


def status_snapshot(conn, division):
    if type(division) is not int or division <= 0:
        raise ValueError("invalid_division")
    roles = conn.execute("""SELECT role,desired_owner,active_owner,lease_until,
        heartbeat_at,draining,status,detail FROM jnp_worker_roles
        WHERE division=%s ORDER BY role""", (division,)).fetchall()
    budgets = conn.execute("""SELECT connection,daily_limit,daily_remaining,daily_reset_ms,
        minute_limit,minute_remaining,minute_reset_ms,observed_at
        FROM jnp_exact_api_budget WHERE division=%s ORDER BY connection""", (division,)).fetchall()
    now = utcnow()
    owner_label = lambda value: hashlib.sha256(value.encode()).hexdigest()[:12] if value else None
    return {
        "roles": [{"role": r[0], "desired_owner": owner_label(r[1]), "active_owner": owner_label(r[2]),
                   "lease_until": r[3].isoformat() if r[3] else None,
                   "heartbeat_at": r[4].isoformat() if r[4] else None,
                   "draining": r[5], "status": r[6], "detail": r[7],
                   "lease_live": bool(r[3] and r[3] > now)} for r in roles],
        "api_budgets": [{"connection": r[0], "daily_limit": r[1], "daily_remaining": r[2],
                         "daily_reset_ms": r[3], "minute_limit": r[4], "minute_remaining": r[5],
                         "minute_reset_ms": r[6], "observed_at": r[7].isoformat() if r[7] else None}
                        for r in budgets],
    }


class DurableRoleLease:
    """Async process wrapper around the short synchronous lease transactions."""
    def __init__(self, database_url, division, role, *, owner=None, lease_seconds=90):
        if not database_url:
            raise ValueError("database_required_for_worker")
        validate_identity(division, "main", role)
        self.database_url, self.division, self.role = database_url, division, role
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        self.lease_seconds = lease_seconds
        self.lease_id = None
        self.lost = asyncio.Event()
        self._stop = asyncio.Event()
        self._heartbeat_task = None

    def _connection_call(self, function, *args, **kwargs):
        import psycopg
        with psycopg.connect(self.database_url, autocommit=True, connect_timeout=10) as conn:
            initialize(conn)
            return function(conn, *args, **kwargs)

    async def start(self):
        self.lease_id = await asyncio.to_thread(
            self._connection_call, claim_role, self.division, self.role, self.owner,
            lease_seconds=self.lease_seconds)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="jnp:role-heartbeat:" + self.role)
        return self

    async def _heartbeat_loop(self):
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(5, self.lease_seconds // 3))
                    return
                except asyncio.TimeoutError:
                    pass
                result = await asyncio.to_thread(
                    self._connection_call, heartbeat, self.division, self.role,
                    self.owner, self.lease_id, "ready", lease_seconds=self.lease_seconds)
                if result["draining"]:
                    self.lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost.set()

    async def mark_draining(self):
        if self.lease_id is None:
            return
        try:
            await asyncio.to_thread(self._connection_call, heartbeat, self.division, self.role,
                                    self.owner, self.lease_id, "draining", lease_seconds=self.lease_seconds)
        except Exception:
            self.lost.set()

    async def close(self, status="stopped"):
        self._stop.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        if self.lease_id is not None:
            try:
                await asyncio.to_thread(self._connection_call, release_role, self.division, self.role,
                                        self.owner, self.lease_id, status=status)
            except LeaseUnavailable:
                pass
            self.lease_id = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, exc_type, _exc, _tb):
        await self.close("error" if exc_type else "stopped")
