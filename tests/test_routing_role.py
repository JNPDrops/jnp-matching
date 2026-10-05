"""Routing lifecycle tests use only synthetic tasks, never Exact."""
import asyncio
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from operations import routing_role as r, task_drain
from operations.worker_write_fence import _owner


class RoutingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def lease(self):
        return SimpleNamespace(lost=asyncio.Event(), mark_draining=AsyncMock(), close=AsyncMock())

    async def test_drain_finishes_current_operation_with_owner_then_does_not_start_next(self):
        stop, entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lease = self.lease()
        events = []
        async def task(_):
            self.assertIs(_owner.get(), lease)
            while not task_drain.requested():
                entered.set()
                events.append('write')
                await finish.wait()
                events.append('audit')
        runner = asyncio.create_task(r.owned_run(object(), stop, lease, task))
        await entered.wait()
        stop.set()
        for _ in range(5): await asyncio.sleep(0)
        self.assertFalse(runner.done())
        finish.set()
        await asyncio.wait_for(runner, 1)
        self.assertEqual(events, ['write', 'audit'])
        self.assertIsNone(_owner.get())
        lease.mark_draining.assert_awaited_once()

    async def test_lease_loss_stops_at_boundary(self):
        lease, stop, entered = self.lease(), asyncio.Event(), asyncio.Event()
        async def task(_):
            entered.set()
            await task_drain.wait(3600)
            self.assertTrue(task_drain.requested())
        runner = asyncio.create_task(r.owned_run(object(), stop, lease, task))
        await entered.wait()
        lease.lost.set()
        await asyncio.wait_for(runner, 1)

    async def test_stuck_operation_is_cancelled_and_failure_is_visible(self):
        lease, stop, entered, cancelled = self.lease(), asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def task(_):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        runner = asyncio.create_task(r.owned_run(object(), stop, lease, task, drain_seconds=.01))
        await entered.wait()
        stop.set()
        with self.assertRaisesRegex(RuntimeError, 'drain_timeout_review_required'):
            await asyncio.wait_for(runner, 1)
        self.assertTrue(cancelled.is_set())

    async def test_unassigned_worker_does_not_invoke_routing(self):
        stop = asyncio.Event()
        lease = self.lease()
        async def start():
            stop.set()
            raise r.c.LeaseUnavailable('role_not_assigned')
        lease.start = start
        task = AsyncMock()
        await r.supervise(SimpleNamespace(DATABASE_URL='synthetic', DIVISION=3977752),
                          stop, r.WORKER, task, lease_factory=lambda *_: lease)
        task.assert_not_awaited()
        lease.close.assert_not_awaited()

    async def test_worker_never_falls_back_to_uncoordinated_execution(self):
        task = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'database_required'):
            await r.supervise(SimpleNamespace(DATABASE_URL=''), asyncio.Event(), r.WORKER, task)
        task.assert_not_awaited()

    async def test_task_failure_releases_as_error_without_restart(self):
        lease, stop = self.lease(), asyncio.Event()
        lease.start = AsyncMock()
        task = AsyncMock(side_effect=ValueError('synthetic'))
        with self.assertRaises(ValueError):
            await r.supervise(SimpleNamespace(DATABASE_URL='synthetic', DIVISION=3977752),
                              stop, r.WORKER, task, lease_factory=lambda *_: lease)
        task.assert_awaited_once()
        lease.close.assert_awaited_once_with('error')

    async def test_routing_entrypoint_sigterm_reaches_supervisor_and_removes_handlers(self):
        from app.worker import run_routing
        loop, handlers = asyncio.get_running_loop(), {}
        app = object()
        async def supervise(actual, stop, owner, function):
            self.assertIs(actual, app)
            self.assertEqual(owner, r.WORKER)
            handlers[signal.SIGTERM]()
            self.assertTrue(stop.is_set())
        with patch.object(r, 'supervise', side_effect=supervise), \
             patch.object(loop, 'add_signal_handler', side_effect=lambda s,cb: handlers.update({s:cb})), \
             patch.object(loop, 'remove_signal_handler') as remove:
            await run_routing(app)
        self.assertCountEqual([call.args[0] for call in remove.call_args_list], [signal.SIGTERM, signal.SIGINT])


if __name__ == '__main__': unittest.main()
