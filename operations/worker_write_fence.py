"""Fail-closed write admission for owned tasks, with durable unresolved intents.

A lease timeout cannot prove that an HTTP write did not execute. Keep admission
durable until the owning operation has persisted its own acknowledgement/audit.
There is deliberately no timeout-based clearing or administrative force retry.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import asyncio
from uuid import uuid4

from operations import worker_coordination as c

_owner = ContextVar('jnp_write_owner', default=None)
_operation = ContextVar('jnp_write_operation', default=None)
_job = ContextVar('jnp_worker_job', default=None)


class WriteFenced(c.CoordinationError):
    pass


@dataclass
class Operation:
    operation_id: str
    lease: object
    admitted: bool = False
    acknowledged: bool = False


@contextmanager
def owner_scope(lease):
    """Child asyncio tasks inherit the owner; unrelated tasks do not."""
    token = _owner.set(lease)
    try:
        yield
    finally:
        _owner.reset(token)


def validate_context(database_url, division):
    lease = _owner.get()
    if lease is not None and (not database_url or database_url != lease.database_url
                              or division != lease.division):
        raise WriteFenced('worker_connection_scope_mismatch')


def current_owner():
    return _owner.get()


@contextmanager
def job_scope(job_id):
    token = _job.set(job_id)
    try:
        yield
    finally:
        _job.reset(token)


def current_job():
    return _job.get()


def audit_metadata():
    operation = _operation.get()
    result = {'worker_operation_id': operation.operation_id} if operation is not None else {}
    if _job.get(): result['worker_job_id'] = _job.get()
    return result


def admit_write(conn, operation, connection, method):
    lease = operation.lease
    c.validate_identity(lease.division, connection, lease.role)
    if method not in {'POST', 'PUT', 'DELETE'}:
        raise ValueError('invalid_write_method')
    with conn.transaction():
        c.lock(conn, lease.division, 'role:' + lease.role)
        # Use the database clock at admission, not a worker's wall clock.
        row = conn.execute("""SELECT active_owner,lease_id,draining,
            lease_until > clock_timestamp() FROM jnp_worker_roles
            WHERE division=%s AND role=%s FOR UPDATE""",
            (lease.division, lease.role)).fetchone()
        if not row or row[0] != lease.owner or str(row[1]) != str(lease.lease_id) or row[2] or not row[3]:
            raise WriteFenced('role_no_longer_owns_write')
        if conn.execute("""SELECT 1 FROM jnp_worker_writes WHERE division=%s
            AND role=%s AND state='unresolved' LIMIT 1""",
            (lease.division, lease.role)).fetchone():
            raise WriteFenced('prior_write_requires_review')
        conn.execute("""INSERT INTO jnp_worker_writes
            (operation_id,division,role,lease_id,connection,method)
            VALUES(%s,%s,%s,%s,%s,%s)""", (operation.operation_id, lease.division,
            lease.role, lease.lease_id, connection, method))


def settle_write(conn, operation):
    lease = operation.lease
    with conn.transaction():
        c.lock(conn, lease.division, 'role:' + lease.role)
        row = conn.execute("""UPDATE jnp_worker_writes SET state='settled',settled_at=NOW()
            WHERE operation_id=%s AND division=%s AND role=%s AND lease_id=%s
            AND state='unresolved' RETURNING operation_id""",
            (operation.operation_id, lease.division, lease.role, lease.lease_id)).fetchone()
        if row is None:
            raise WriteFenced('write_settlement_missing')


def owned_operation(role):
    """Fence one write until the decorated operation's durable audit completes.

Adapters must not swallow failed audit persistence or launch unawaited writes.
The initial adapter is customer-only routing, which persists customer_applied
before returning. Unadapted owned writers are denied by fenced_send.
"""
    def decorate(function):
        @wraps(function)
        async def run(*args, **kwargs):
            lease = _owner.get()
            if lease is None:
                return await function(*args, **kwargs)  # unchanged legacy runtime
            if lease.role != role or _operation.get() is not None or lease.lost.is_set():
                raise WriteFenced('invalid_owned_operation')
            operation = Operation(str(uuid4()), lease)
            token = _operation.set(operation)
            try:
                result = await function(*args, **kwargs)
                if operation.admitted and operation.acknowledged:
                    await asyncio.to_thread(c._budget_database_call, lease.database_url,
                                            settle_write, operation)
                elif operation.admitted:
                    lease.lost.set()
                return result
            except BaseException:
                # Stop this owner at the next boundary instead of spending
                # more API quota on a role whose last write needs review.
                if operation.admitted:
                    lease.lost.set()
                raise
            finally:
                _operation.reset(token)
        return run
    return decorate


async def fenced_send(database_url, division, connection, method, send):
    lease = _owner.get()
    validate_context(database_url, division)
    if lease is not None and lease.role == "reports" and method.upper() != "GET":
        raise WriteFenced("read_only_role")
    if lease is None or method.upper() == 'GET':
        return await send()
    operation = _operation.get()
    if (operation is None or operation.lease is not lease or operation.admitted
            or lease.lost.is_set() or lease._stop.is_set()):
        raise WriteFenced('write_operation_not_admitted')
    # If cancellation or a database disconnect makes admission uncertain, leave
    # the durable intent unresolved. Never send a request after failed admission.
    operation.admitted = True
    await asyncio.to_thread(c._budget_database_call, database_url, admit_write,
                            operation, connection, method.upper())
    if lease.lost.is_set() or lease._stop.is_set():
        raise WriteFenced('owner_stopped_after_admission')
    response = await send()
    operation.acknowledged = 200 <= response.status_code < 300
    return response


async def browser_save(app, role, save):
    """Fence one UI save; the surrounding adapter must verify and persist outcome.

    A completed click is not proof of bookkeeping. Only the outer operation's
    successful business readback permits settlement of the admitted write.
    """
    from types import SimpleNamespace
    lease = current_owner()
    if lease is not None and lease.role != role:
        raise WriteFenced('wrong_browser_write_role')
    async def send():
        await save()
        return SimpleNamespace(status_code=200)
    return await fenced_send(app.DATABASE_URL,app.DIVISION,'main','POST',send)
