import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.runtime import BackgroundTasks, ROLES, SeparationNotReady, TASKS, background_tasks, role_manifest


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_task_arguments_and_shutdown(self):
        seen, stopped = [], []
        app = object()

        def resolver(spec):
            async def task(*args):
                seen.append((spec.name, args))
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.append(spec.name)
            return task

        runtime = BackgroundTasks(app, resolver=resolver)
        await runtime.start()
        await asyncio.sleep(0)
        self.assertEqual(len(seen), 12)
        self.assertEqual(seen, [(spec.name, (app,) if spec.with_app else ()) for spec in TASKS])
        with self.assertRaisesRegex(RuntimeError, "already_started"):
            await runtime.start()
        await runtime.stop()
        self.assertCountEqual(stopped, [spec.name for spec in TASKS])

    async def test_import_failure_starts_no_partial_owners(self):
        started = []

        def resolver(spec):
            if spec.name == "icepay-transactions":
                raise ImportError("missing module")
            async def task(*_):
                started.append(spec.name)
            return task

        runtime = BackgroundTasks(None, resolver=resolver)
        with self.assertRaises(ImportError):
            await runtime.start()
        await asyncio.sleep(0)
        self.assertEqual(started, [])
        self.assertEqual(runtime.tasks, {})

    async def test_failed_task_does_not_prevent_other_cleanup_or_leak_message(self):
        stopped = []

        def resolver(spec):
            async def task(*_):
                if spec.name == "debtor-routing":
                    raise ValueError("SENSITIVE_PROVIDER_BODY")
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.append(spec.name)
            return task

        runtime = BackgroundTasks(None, resolver=resolver)
        await runtime.start()
        await asyncio.sleep(0)
        with self.assertLogs("uvicorn.error", level="ERROR") as messages:
            await runtime.stop()
        self.assertEqual(len(stopped), 11)
        self.assertNotIn("SENSITIVE_PROVIDER_BODY", str(messages.output))
        self.assertIn("ValueError", str(messages.output))

    async def test_fibonatix_shutdown_failure_still_stops_lifespan_tasks(self):
        with patch("app.runtime.BackgroundTasks") as factory, patch(
            "operations.fibonatix_import.shutdown", new=AsyncMock(side_effect=RuntimeError("shutdown"))
        ):
            runtime = factory.return_value
            runtime.start = AsyncMock()
            runtime.stop = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, "shutdown"):
                async with background_tasks(None):
                    pass
            runtime.stop.assert_awaited_once()

    async def test_no_split_role_can_accidentally_execute(self):
        for role in ROLES:
            with self.subTest(role=role):
                self.assertFalse(role_manifest(role)["execution_ready"])
                with self.assertRaises(SeparationNotReady):
                    BackgroundTasks(None, role)

    async def test_worker_refuses_before_importing_the_financial_app(self):
        from app.worker import run
        with patch("app.runtime.import_module", side_effect=AssertionError("unexpected import")):
            with self.assertRaises(SeparationNotReady):
                await run("routing")

    async def test_worker_wakes_on_stop_and_does_not_restart_finished_job(self):
        from app.worker import wait_for_stop
        calls = []

        async def one_shot():
            calls.append("once")

        stop = asyncio.Event()
        task = asyncio.create_task(one_shot())
        runtime = SimpleNamespace(tasks={"once": task}, specs=[])
        waiter = asyncio.create_task(wait_for_stop(runtime, stop))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        stop.set()
        await waiter
        self.assertEqual(calls, ["once"])

    async def test_worker_exits_if_continuous_owner_ends(self):
        from app.worker import wait_for_stop
        task = asyncio.create_task(asyncio.sleep(0))
        runtime = SimpleNamespace(tasks={"routing": task}, specs=[SimpleNamespace(name="routing", continuous=True)])
        with self.assertRaisesRegex(RuntimeError, "continuous_task_exited:routing"):
            await wait_for_stop(runtime, asyncio.Event())

    async def test_worker_error_does_not_expose_provider_message(self):
        from app.worker import wait_for_stop

        async def failed():
            raise ValueError("PRIVATE_RESPONSE")

        task = asyncio.create_task(failed())
        runtime = SimpleNamespace(tasks={"once": task}, specs=[])
        with self.assertRaisesRegex(RuntimeError, "background_task_failed:once") as error:
            await wait_for_stop(runtime, asyncio.Event())
        self.assertNotIn("PRIVATE_RESPONSE", str(error.exception))

    async def test_worker_stops_when_durable_lease_is_lost(self):
        from app.worker import wait_for_stop
        task = asyncio.create_task(asyncio.Event().wait())
        lost = asyncio.Event()
        runtime = SimpleNamespace(role="routing", tasks={"routing":task},
                                  specs=[SimpleNamespace(name="routing", continuous=True)])
        waiter = asyncio.create_task(wait_for_stop(runtime, asyncio.Event(), lost))
        lost.set()
        with self.assertRaisesRegex(RuntimeError, "role_lease_lost:routing"):
            await waiter
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_headless_signal_stops_and_releases_handlers(self):
        import signal
        from app.worker import run
        loop = asyncio.get_running_loop()
        handlers = {}
        runtime = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), tasks={}, specs=[])
        lease = SimpleNamespace(start=AsyncMock(), mark_draining=AsyncMock(), close=AsyncMock(), lost=asyncio.Event())

        async def started():
            handlers[signal.SIGTERM]()

        runtime.start.side_effect = started
        with patch("app.worker.require_ready"), patch("app.worker.BackgroundTasks", return_value=runtime), \
             patch("operations.worker_coordination.DurableRoleLease", return_value=lease), \
             patch.object(loop, "add_signal_handler", side_effect=lambda sig, cb: handlers.update({sig: cb})), \
             patch.object(loop, "remove_signal_handler") as remove:
            await run("routing")
        runtime.stop.assert_awaited_once()
        lease.start.assert_awaited_once()
        lease.mark_draining.assert_awaited_once()
        lease.close.assert_awaited_once_with("stopped")
        self.assertCountEqual([args.args[0] for args in remove.call_args_list], [signal.SIGTERM, signal.SIGINT])


class CommandTests(unittest.TestCase):
    def test_check_works_with_no_dependencies_or_credentials(self):
        # -S prevents site packages, proving --check cannot reach financial
        # implementations (which require FastAPI/psycopg) or open a database.
        result = subprocess.run([sys.executable, "-S", "-m", "app.worker", "--role", "routing", "--check"],
                                capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        self.assertFalse(data["execution_ready"])
        self.assertEqual(data["tasks"], ["debtor-routing"])

    def test_headless_execution_exits_nonzero_before_loading_app(self):
        result = subprocess.run([sys.executable, "-S", "-m", "app.worker", "--role", "icepay"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("role_not_ready:icepay", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
