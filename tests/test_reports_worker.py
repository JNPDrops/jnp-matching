import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from operations import reports_jobs as reports, worker_write_fence as fence


class ReportWorkerTests(unittest.IsolatedAsyncioTestCase):
    def task(self, result=True, **kwargs):
        return reports.Task(SimpleNamespace(run=AsyncMock(return_value=result)), 'fixture',
                            datetime.now(timezone.utc)+timedelta(hours=1), **kwargs)

    async def test_existing_claim_or_failed_report_is_not_completion(self):
        for result in (None, False):
            task=self.task(result)
            with patch.object(reports,'catalog',return_value={'fixture':task}):
                with self.assertRaisesRegex(ValueError,'not_completed'):
                    await reports.dispatch(None,{'task_key':'fixture','action':'run','params':{}})

    async def test_successful_report_is_acknowledged(self):
        task=self.task()
        with patch.object(reports,'catalog',return_value={'fixture':task}):
            await reports.dispatch(None,{'task_key':'fixture','action':'run','params':{}})
        task.module.run.assert_awaited_once_with(None)

    async def test_probe_identity_and_durable_completion(self):
        task=self.task(query='SELECT result',activation='PROBE')
        with patch.object(reports,'catalog',return_value={'fixture':task}), \
             patch.object(reports,'probe_completed') as confirmed:
            await reports.dispatch(None,{'task_key':'fixture','action':'run','params':{}})
        task.module.run.assert_awaited_once_with(task_id='fixture')
        confirmed.assert_called_once_with(None,task)

    def test_expired_and_arbitrary_read_jobs_rejected(self):
        task=reports.Task(SimpleNamespace(), 'fixture', datetime.now(timezone.utc)-timedelta(seconds=1))
        with patch.object(reports,'catalog',return_value={'fixture':task}):
            for action,params in [('run',{}),('exec',{}),('run',{'url':'https://example.invalid'})]:
                with self.assertRaises(ValueError):
                    reports.validate({'task_key':'fixture','action':action,'params':params})

    async def test_reports_role_refuses_all_financial_http_methods(self):
        lease=SimpleNamespace(role='reports',database_url='synthetic',division=3977752)
        with fence.owner_scope(lease):
            for method in ('POST','PUT','DELETE','PATCH'):
                send=AsyncMock()
                with self.assertRaisesRegex(fence.WriteFenced,'read_only_role'):
                    await fence.fenced_send('synthetic',3977752,'main',method,send)
                send.assert_not_awaited()
            send=AsyncMock(return_value='read')
            self.assertEqual(await fence.fenced_send('synthetic',3977752,'main','GET',send),'read')

    async def test_reports_role_cannot_use_browser_save(self):
        app=SimpleNamespace(DATABASE_URL='synthetic',DIVISION=3977752)
        lease=SimpleNamespace(role='reports',database_url='synthetic',division=3977752)
        click=AsyncMock()
        with fence.owner_scope(lease),self.assertRaises(fence.WriteFenced):
            await fence.browser_save(app,'reports',click)
        click.assert_not_awaited()

    def test_failed_probe_record_is_not_completion(self):
        app=MagicMock()
        app._db_connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=('failed',)
        with self.assertRaisesRegex(ValueError,'no_confirmed'):
            reports.probe_completed(app,self.task(query='SELECT result'))
