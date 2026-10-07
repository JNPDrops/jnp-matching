"""Synthetic supervisor/consumer regression; no database or financial writes."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from operations import agent_jobs, assigned_role, worker_coordination as coordination


class QueueDrainRace(unittest.IsolatedAsyncioTestCase):
    def lease(self, role):
        lease = SimpleNamespace(role=role, lost=asyncio.Event(), drains=0)

        async def mark_draining():
            lease.drains += 1

        lease.mark_draining = mark_draining
        return lease

    async def test_claim_observes_drain_before_heartbeat(self):
        for role in ('fibonatix', 'icepay', 'reports'):
            with self.subTest(role=role):
                lease = self.lease(role)
                dispatched = []

                def database_call(app, function, *args):
                    if function is agent_jobs.claim_next:
                        raise coordination.LeaseUnavailable('role_not_available_for_job')

                async def dispatch(job):
                    dispatched.append(job)

                async def consumer(app):
                    await agent_jobs.consume(app, role, dispatch)

                with patch.object(agent_jobs, 'database_call', database_call):
                    await assigned_role.owned_run(None, asyncio.Event(), lease, consumer)
                self.assertTrue(lease.lost.is_set())
                self.assertEqual(lease.drains, 1)
                self.assertEqual(dispatched, [])

    async def test_real_unexpected_return_still_fails(self):
        async def premature(app):
            return

        with self.assertRaisesRegex(RuntimeError, 'fibonatix_task_exited_unexpectedly'):
            await assigned_role.owned_run(None, asyncio.Event(), self.lease('fibonatix'), premature)

    async def test_real_exception_is_not_swallowed(self):
        async def broken(app):
            raise ValueError('synthetic_failure')

        with self.assertRaisesRegex(ValueError, 'synthetic_failure'):
            await assigned_role.owned_run(None, asyncio.Event(), self.lease('reports'), broken)


if __name__ == '__main__':
    unittest.main()
