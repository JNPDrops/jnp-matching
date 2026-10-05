"""Single routing owner shared by the legacy web lifespan and headless worker."""
import asyncio

from operations import task_drain
from operations import worker_coordination as c
from operations.worker_write_fence import owner_scope

LEGACY = 'legacy-routing'
WORKER = 'worker-routing'
OWNERS = frozenset({LEGACY, WORKER})
DRAIN_SECONDS = 240


def initialize_assignment(conn, division):
    c.validate_identity(division, 'allocation', 'routing')
    with conn.transaction():
        c.lock(conn, division, 'role:routing')
        # The first standalone worker waits; only the existing web role is
        # assigned by default. Never replace an existing pause or handover.
        conn.execute("""INSERT INTO jnp_worker_roles(division,role,desired_owner)
            VALUES(%s,'routing',%s) ON CONFLICT DO NOTHING""", (division, LEGACY))


def request_handover(conn, division, target):
    if target not in OWNERS and target is not None:
        raise ValueError('invalid_routing_target')
    initialize_assignment(conn, division)
    with conn.transaction():
        c.lock(conn, division, 'role:routing')
        row = conn.execute("""SELECT desired_owner FROM jnp_worker_roles
            WHERE division=%s AND role='routing' FOR UPDATE""", (division,)).fetchone()
        if row[0] == target:
            return False  # repeating the command must not drain its new owner
        c.begin_drain(conn, division, 'routing', desired_owner=target)
        return True


class RoutingLease(c.DurableRoleLease):
    def __init__(self, database_url, division, owner):
        if owner not in OWNERS:
            raise ValueError('invalid_routing_owner')
        super().__init__(database_url, division, 'routing', owner=owner, require_assigned=True)

    async def start(self):
        await asyncio.to_thread(self._connection_call, initialize_assignment, self.division)
        return await super().start()


async def sleep_until_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def owned_run(app, stop, lease, function, *, drain_seconds=DRAIN_SECONDS):
    """Keep the owner until the current routing entry has checkpointed."""
    drain = asyncio.Event()
    with owner_scope(lease):
        task = asyncio.create_task(task_drain.run(drain, function, app), name='jnp:routing-owned')
    stopped = asyncio.create_task(stop.wait())
    lost = asyncio.create_task(lease.lost.wait())
    try:
        done, _ = await asyncio.wait({task, stopped, lost}, return_when=asyncio.FIRST_COMPLETED)
        if task in done and not stop.is_set() and not lease.lost.is_set():
            await task
            raise RuntimeError('routing_task_exited_unexpectedly')
    finally:
        drain.set()
        try:
            await lease.mark_draining()
            done, _ = await asyncio.wait({task}, timeout=drain_seconds)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise RuntimeError('routing_drain_timeout_review_required')
            await task
        finally:
            stopped.cancel()
            lost.cancel()
            await asyncio.gather(stopped, lost, return_exceptions=True)


async def supervise(app, stop, owner, function, *, lease_factory=RoutingLease, retry_seconds=5,
                    drain_seconds=DRAIN_SECONDS):
    database_url = getattr(app, 'DATABASE_URL', '')
    if not database_url:
        if owner != LEGACY:
            raise ValueError('database_required_for_worker')
        return await task_drain.run(stop, function, app)  # existing local mode
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
        # On assignment change stay alive but idle, preserving all queues and
        # cursors. No restart of unrelated web/PSP roles is required.
        await sleep_until_stop(stop, retry_seconds)
