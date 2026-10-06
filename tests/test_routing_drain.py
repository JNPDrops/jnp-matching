import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from operations import task_drain as drain
from operations import automatic_debtor_routing as routing


class DrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_finishes_inflight_cycle_without_next_cycle(self):
        entered, finish, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def cycle(_):
            entered.set()
            await finish.wait()
        with patch.object(routing, 'cycle', new=AsyncMock(side_effect=cycle)) as run, \
             patch('operations.requested_route_diagnostic.run', new=AsyncMock()):
            task = asyncio.create_task(drain.run(stop, routing.serve, object()))
            await entered.wait()
            stop.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            await asyncio.wait_for(task, 1)
            run.assert_awaited_once()

    async def test_stop_before_start_has_no_db_or_diagnostic_calls(self):
        stop = asyncio.Event()
        stop.set()
        with patch('operations.requested_route_diagnostic.run', new=AsyncMock()) as diagnostic:
            await drain.run(stop, routing.serve, object())
            await drain.run(stop, routing.cycle, object())
            diagnostic.assert_not_awaited()

    async def test_context_isolated_and_wait_interruptible(self):
        stop = asyncio.Event()
        entered = asyncio.Event()
        async def task():
            entered.set()
            await drain.wait(3600)
            self.assertTrue(drain.requested())
        pending = asyncio.create_task(drain.run(stop, task))
        await entered.wait()
        stop.set()
        self.assertFalse(drain.requested())
        await asyncio.wait_for(pending, 1)
