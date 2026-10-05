"""Assigned role ownership shared by a legacy web process and headless workers."""
import asyncio

from operations import task_drain
from operations import worker_coordination as c
from operations.worker_write_fence import owner_scope

ROLES = frozenset({'routing', 'woo-rules'})
DRAIN_SECONDS = 240


def owner_for(role, location):
    if role not in ROLES or location not in {'web', 'worker'}:
        raise ValueError('invalid_assigned_role')
    return ('legacy-' if location == 'web' else 'worker-') + role


def initialize_assignment(conn, division, role):
    default = owner_for(role, 'web')
    c.validate_identity(division, 'main', role)
    with conn.transaction():
        c.lock(conn, division, 'role:' + role)
        conn.execute('''INSERT INTO jnp_worker_roles(division,role,desired_owner)
            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''', (division, role, default))


def request_handover(conn, division, role, target):
    owners = {owner_for(role, location) for location in ('web', 'worker')}
    if target not in owners and target is not None:
        raise ValueError('invalid_role_target')
    initialize_assignment(conn, division, role)
    with conn.transaction():
        c.lock(conn, division, 'role:' + role)
        row = conn.execute('''SELECT desired_owner FROM jnp_worker_roles
            WHERE division=%s AND role=%s FOR UPDATE''', (division, role)).fetchone()
        if row[0] == target:
            return False
        c.begin_drain(conn, division, role, desired_owner=target)
        return True


class AssignedRoleLease(c.DurableRoleLease):
    def __init__(self, database_url, division, role, owner):
        if owner not in {owner_for(role, location) for location in ('web', 'worker')}:
            raise ValueError('invalid_role_owner')
        super().__init__(database_url, division, role, owner=owner, require_assigned=True)

    async def start(self):
        await asyncio.to_thread(self._connection_call, initialize_assignment, self.division, self.role)
        return await super().start()


async def sleep_until_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def owned_run(app, stop, lease, function, *, drain_seconds=DRAIN_SECONDS):
    """Retain the owner until the in-flight operation has checkpointed."""
    role = getattr(lease, 'role', 'routing')
    drain = asyncio.Event()
    with owner_scope(lease):
        task = asyncio.create_task(task_drain.run(drain, function, app), name='jnp:' + role + '-owned')
    stopped = asyncio.create_task(stop.wait())
    lost = asyncio.create_task(lease.lost.wait())
    try:
        done, _ = await asyncio.wait({task, stopped, lost}, return_when=asyncio.FIRST_COMPLETED)
        if task in done and not stop.is_set() and not lease.lost.is_set():
            await task
            raise RuntimeError(role + '_task_exited_unexpectedly')
    finally:
        drain.set()
        try:
            await lease.mark_draining()
            done, _ = await asyncio.wait({task}, timeout=drain_seconds)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise RuntimeError(role + '_drain_timeout_review_required')
            await task
        finally:
            stopped.cancel()
            lost.cancel()
            await asyncio.gather(stopped, lost, return_exceptions=True)


async def supervise(app, stop, owner, function, *, role, lease_factory=None,
                    retry_seconds=5, drain_seconds=DRAIN_SECONDS):
    owner_for(role, 'web')  # validate even in local mode
    if owner not in {owner_for(role, location) for location in ('web', 'worker')}:
        raise ValueError('invalid_role_owner')
    database_url = getattr(app, 'DATABASE_URL', '')
    if not database_url:
        if owner != owner_for(role, 'web'):
            raise ValueError('database_required_for_worker')
        return await task_drain.run(stop, function, app)
    if lease_factory is None:
        lease_factory = lambda url, division, owner: AssignedRoleLease(url, division, role, owner)
    while not stop.is_set():
        lease = lease_factory(database_url, app.DIVISION, owner)
        try:
            await lease.start()
        except c.LeaseUnavailable:
            await sleep_until_stop(stop, retry_seconds)
            continue
        failed = False
        try:
            if not stop.is_set():
                await owned_run(app, stop, lease, function, drain_seconds=drain_seconds)
        except BaseException:
            failed = True
            raise
        finally:
            await lease.close('error' if failed else 'stopped')
        await sleep_until_stop(stop, retry_seconds)
