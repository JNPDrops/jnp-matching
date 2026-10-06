import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from operations import agent_jobs as jobs, fibonatix_jobs as fibo
from operations import worker_write_fence as fence, worker_coordination as c


class QueueExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lease=SimpleNamespace(role='fibonatix',lost=asyncio.Event(),database_url='synthetic',
            division=3977752,owner='legacy-fibonatix',lease_id=str(uuid4()),_stop=asyncio.Event())
        self.job={'job_id':str(uuid4()),'action':'preflight','task_key':'fixture','params':{}}
    async def test_completed_and_failed_dispatch_persist_once_without_retry(self):
        for error,state in [(None,'completed'),(ValueError('PRIVATE'),'blocked')]:
            dispatch=AsyncMock(side_effect=error)
            with patch.object(jobs,'database_call') as store:
                await jobs.execute(None,self.lease,self.job,dispatch)
            dispatch.assert_awaited_once_with(self.job)
            self.assertEqual(store.call_args.args[4],state)
            self.assertNotIn('PRIVATE',str(store.call_args))
    async def test_uncertain_write_never_marks_job_complete(self):
        self.lease.lost.set()
        with patch.object(jobs,'database_call') as store:
            await jobs.execute(None,self.lease,self.job,AsyncMock())
        self.assertEqual(store.call_args.args[4:6],('uncertain','write_requires_review'))
    async def test_cancellation_marks_uncertain_and_propagates(self):
        with patch.object(jobs,'database_call') as store:
            with self.assertRaises(asyncio.CancelledError):
                await jobs.execute(None,self.lease,self.job,AsyncMock(side_effect=asyncio.CancelledError()))
        self.assertEqual(store.call_args.args[4:6],('uncertain','worker_interrupted'))
    async def test_job_identity_is_scoped_and_restored(self):
        async def dispatch(job):self.assertEqual(fence.current_job(),job['job_id'])
        with patch.object(jobs,'database_call'):
            await jobs.execute(None,self.lease,self.job,dispatch)
        self.assertIsNone(fence.current_job())
    async def test_browser_save_needs_owned_operation_and_full_business_confirmation(self):
        events=[]
        def database(url,function,*args,**kwargs):events.append(function.__name__)
        click=AsyncMock()
        app=SimpleNamespace(DATABASE_URL='synthetic',DIVISION=3977752)
        @fence.owned_operation('fibonatix')
        async def operation(fail=False):
            await fence.browser_save(app,'fibonatix',click)
            if fail:raise ValueError('readback failed')
            events.append('business_checkpoint')
        with patch.object(c,'_budget_database_call',side_effect=database),fence.owner_scope(self.lease):
            with self.assertRaises(fence.WriteFenced):await fence.browser_save(app,'fibonatix',click)
            click.assert_not_awaited()
            await operation()
            self.assertEqual(events,['admit_write','business_checkpoint','settle_write'])
            events.clear()
            with self.assertRaises(ValueError):await operation(True)
        self.assertEqual(events,['admit_write'])
        self.assertTrue(self.lease.lost.is_set())


class FibonatixCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_commands_do_not_allow_retired_actions_or_arbitrary_parameters(self):
        for action,params in [('automatic',{}),('settle_48189',{}),('strict_match',{'limit':6}),
                              ('strict_match',{'limit':True}),('import',{'url':'synthetic'}),
                              ('strict_match',{'limit':1,'division':1})]:
            with self.assertRaises(ValueError):fibo.validate(action,params)
        fibo.validate('strict_match',{'limit':1});fibo.validate('preflight',{})
    async def test_expired_or_wrong_historical_batch_never_dispatches(self):
        from operations import fibonatix_import as legacy
        with patch.object(legacy,'run',new_callable=AsyncMock) as run:
            with self.assertRaises(ValueError):await fibo.dispatch({'action':'import','params':{},'task_key':'wrong'})
            with patch.object(legacy,'EXPIRES',datetime.now(timezone.utc)-timedelta(seconds=1)):
                with self.assertRaises(ValueError):await fibo.dispatch({'action':'import','params':{},'task_key':legacy.JOB})
            run.assert_not_awaited()
    async def test_http_start_only_enqueues(self):
        from operations import fibonatix_import as legacy
        request=MagicMock()
        with patch.object(legacy,'authorize'),patch.object(fibo,'submit',return_value={'job_id':'synthetic','state':'queued'}),patch.object(legacy,'run',new_callable=AsyncMock) as run:
            result=await legacy.start('preflight',request)
        self.assertEqual(result['state'],'queued');run.assert_not_awaited()
    async def test_queued_runner_propagates_error_instead_of_false_completion(self):
        from operations import fibonatix_import as legacy
        conn=MagicMock();conn.execute.return_value.fetchone.return_value=(True,)
        with patch.object(legacy,'database',return_value=conn),patch.object(legacy,'update'), \
             patch.object(legacy,'preflight',side_effect=ValueError('synthetic')),fence.job_scope('synthetic'):
            with self.assertRaises(ValueError):await legacy.run('preflight')
