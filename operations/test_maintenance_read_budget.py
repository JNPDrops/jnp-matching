import unittest
from unittest.mock import AsyncMock, patch
from operations import allocation_maintenance as m


class ReadBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_continues_after_minute_reset(self):
        send=AsyncMock(side_effect=[m.BudgetDeferred('minute_reserve'),'result'])
        with patch.object(m.task_drain,'wait',AsyncMock(return_value=False)) as wait:
            self.assertEqual(await m.read_budget_retry('GET',send),'result')
        self.assertEqual(send.await_count,2)
        wait.assert_awaited_once_with(60)

    async def test_unknown_minute_budget_is_bounded(self):
        send=AsyncMock(side_effect=m.BudgetDeferred('unknown_minute_cap'))
        with patch.object(m.task_drain,'wait',AsyncMock(return_value=False)) as wait:
            with self.assertRaises(m.BudgetDeferred):
                await m.read_budget_retry('GET',send)
        self.assertEqual(send.await_count,3)
        self.assertEqual(wait.await_count,2)

    async def test_writes_never_retry(self):
        for method in ('POST','PUT','DELETE'):
            send=AsyncMock(side_effect=m.BudgetDeferred('minute_reserve'))
            with patch.object(m.task_drain,'wait',AsyncMock()) as wait:
                with self.assertRaises(m.BudgetDeferred):
                    await m.read_budget_retry(method,send)
            self.assertEqual(send.await_count,1)
            wait.assert_not_awaited()

    async def test_daily_quota_and_transport_failure_do_not_retry(self):
        for error in (m.BudgetDeferred('daily_reserve'),TimeoutError()):
            send=AsyncMock(side_effect=error)
            with patch.object(m.task_drain,'wait',AsyncMock()) as wait:
                with self.assertRaises(type(error)):
                    await m.read_budget_retry('GET',send)
            self.assertEqual(send.await_count,1)
            wait.assert_not_awaited()

    async def test_drain_stops_before_second_read(self):
        send=AsyncMock(side_effect=m.BudgetDeferred('minute_reserve'))
        with patch.object(m.task_drain,'wait',AsyncMock(return_value=True)):
            with self.assertRaises(m.MaintenanceDrained):
                await m.read_budget_retry('GET',send)
        self.assertEqual(send.await_count,1)


if __name__=='__main__': unittest.main()
