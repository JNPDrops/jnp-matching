"""Routing compatibility API over the shared assigned-role supervisor."""
from operations import assigned_role as assigned
from operations import worker_coordination as c
from operations.assigned_role import DRAIN_SECONDS, owned_run, sleep_until_stop

LEGACY = assigned.owner_for('routing', 'web')
WORKER = assigned.owner_for('routing', 'worker')
OWNERS = frozenset({LEGACY, WORKER})


def initialize_assignment(conn, division):
    return assigned.initialize_assignment(conn, division, 'routing')


def request_handover(conn, division, target):
    return assigned.request_handover(conn, division, 'routing', target)


class RoutingLease(assigned.AssignedRoleLease):
    def __init__(self, database_url, division, owner):
        super().__init__(database_url, division, 'routing', owner)


async def supervise(app, stop, owner, function, *, lease_factory=RoutingLease,
                    retry_seconds=5, drain_seconds=DRAIN_SECONDS):
    return await assigned.supervise(app, stop, owner, function, role='routing',
        lease_factory=lease_factory, retry_seconds=retry_seconds, drain_seconds=drain_seconds)
