import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import task_drain
from operations import woo_iban_rules as woo
from operations.assigned_role import owner_for, supervise


class WooDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_cycle_opens_no_database(self):
        stop=asyncio.Event(); stop.set()
        app=SimpleNamespace(_db_connect=MagicMock(side_effect=AssertionError('unexpected DB')))
        await task_drain.run(stop,woo.cycle,app)
        await task_drain.run(stop,woo.serve,app)
        app._db_connect.assert_not_called()

    async def test_cycle_retains_lock_until_current_event_finishes_and_skips_next(self):
        entered,finish,stop=asyncio.Event(),asyncio.Event(),asyncio.Event()
        calls=[]
        conn=MagicMock()
        conn.__enter__.return_value=conn
        def execute(sql, args=None):
            calls.append(sql)
            return SimpleNamespace(fetchone=lambda:(True,),fetchall=lambda:[('one',{},'pending'),('two',{},'pending')])
        conn.execute.side_effect=execute
        app=SimpleNamespace(DATABASE_URL='synthetic',DIVISION=woo.DIVISION,BASE_URL=woo.BASE,_db_connect=lambda:conn)
        async def event(*_):
            entered.set()
            await finish.wait()
        with patch.dict('os.environ',{'ENABLE_WOO_IBAN_RULE_WRITES':'true'}), \
             patch.object(woo,'initialize'), patch.object(woo,'ExactAPI'), \
             patch.object(woo,'process',new=AsyncMock(side_effect=event)) as process:
            task=asyncio.create_task(task_drain.run(stop,woo.cycle,app))
            await entered.wait()
            stop.set()
            await asyncio.sleep(0)
            self.assertFalse(any('pg_advisory_unlock' in sql for sql in calls))
            finish.set()
            await asyncio.wait_for(task,1)
            process.assert_awaited_once()
        self.assertIn('pg_advisory_unlock',calls[-1])

    async def test_unassigned_woo_worker_is_idle(self):
        from operations.worker_coordination import LeaseUnavailable
        stop=asyncio.Event()
        async def start():
            stop.set()
            raise LeaseUnavailable('role_not_assigned')
        lease=SimpleNamespace(start=start)
        task=AsyncMock()
        await supervise(SimpleNamespace(DATABASE_URL='synthetic',DIVISION=3977752),stop,
            owner_for('woo-rules','worker'),task,role='woo-rules',lease_factory=lambda *_:lease)
        task.assert_not_awaited()

    async def test_role_stops_do_not_leak_to_other_tasks(self):
        stop_one,stop_two,entered=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def other():
            entered.set()
            await task_drain.wait(.02)
            self.assertFalse(task_drain.requested())
        task=asyncio.create_task(task_drain.run(stop_two,other))
        await entered.wait()
        stop_one.set()
        await task

    async def test_headless_woo_sigterm_reaches_correct_supervisor(self):
        import signal
        from app.worker import run_woo_rules
        loop=asyncio.get_running_loop()
        handlers={}
        async def supervise(app,stop,owner,function,*,role):
            self.assertEqual(role,'woo-rules')
            self.assertEqual(owner,'worker-woo-rules')
            self.assertIs(function,woo.serve)
            handlers[signal.SIGTERM]()
            self.assertTrue(stop.is_set())
        with patch('operations.assigned_role.supervise',side_effect=supervise), \
             patch.object(loop,'add_signal_handler',side_effect=lambda s,cb:handlers.update({s:cb})), \
             patch.object(loop,'remove_signal_handler') as remove:
            await run_woo_rules(object())
        self.assertCountEqual([call.args[0] for call in remove.call_args_list],[signal.SIGTERM,signal.SIGINT])
