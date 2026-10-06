import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from operations import icepay_jobs as jobs, icepay_matching as matching, worker_coordination as c
from operations import worker_write_fence as fence


class IcepayAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_dispatch_does_not_change_environment_or_choose_another_action(self):
        module=SimpleNamespace(EXPIRES=datetime.now(timezone.utc)+timedelta(hours=1),run=AsyncMock())
        task=jobs.Task(module,'fixture','ICEPAY_TRANSACTION_TASK_ID','unused',frozenset({'complete'}))
        with patch.object(jobs,'catalog',return_value={'fixture':task}),patch.object(jobs,'observed_outcome') as observed, \
             patch.dict('os.environ',{'ICEPAY_TRANSACTION_TASK_ID':'different','ICEPAY_MATCH_TASK_ID':'different'}):
            await jobs.dispatch(None,{'task_key':'fixture','action':'run','params':{}})
            self.assertEqual(__import__('os').environ['ICEPAY_TRANSACTION_TASK_ID'],'different')
        module.run.assert_awaited_once_with(task_id='fixture');observed.assert_called_once_with(None,task)
    def test_no_journal_creation_or_arbitrary_command_in_catalog(self):
        from operations import icepay_journal_task as journal
        self.assertNotIn(journal.CREATE_ID,jobs.catalog())
        for job in [{'task_key':journal.CREATE_ID,'action':'run','params':{}},
                    {'task_key':journal.TASK_ID,'action':'run','params':{'url':'bad'}}]:
            with self.assertRaises(ValueError):jobs.validate(job)
    async def test_missing_durable_success_is_never_marked_complete(self):
        task=SimpleNamespace(query='unused',task_id='fixture',success={'done'})
        app=MagicMock();app._db_connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=('blocked',)
        with self.assertRaises(ValueError):jobs.observed_outcome(app,task)
    async def test_explicit_source_task_cannot_trigger_ambient_matching(self):
        from operations import icepay_transactions as source
        with patch.dict('os.environ',{'ICEPAY_MATCH_TASK_ID':'icepay-match-20261001-03-group-01-v1'}), \
             patch.object(matching,'run',new_callable=AsyncMock) as run:
            await source.run(task_id='wrong-source-id')
        run.assert_not_awaited()


class IcepaySaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events=[]
        self.app=SimpleNamespace(DATABASE_URL='synthetic',DIVISION=3977752)
        self.lease=SimpleNamespace(database_url='synthetic',division=3977752,role='icepay',
            owner='legacy-icepay',lease_id=str(uuid4()),lost=asyncio.Event(),_stop=asyncio.Event())
        self.frame=MagicMock();self.frame.locator.return_value.click=AsyncMock(side_effect=lambda:self.events.append('click'))
    def database(self,url,function,*args):self.events.append(function.__name__)
    async def save(self,fail=False):
        async def verify(*args):
            self.events.append('verified')
            if fail:raise ValueError('synthetic readback failure')
        async def persist():self.events.append('persist')
        with patch.object(c,'_budget_database_call',side_effect=self.database), \
             patch.object(matching,'claim_save',side_effect=lambda *args:self.events.append('claim')), \
             patch.object(matching,'verify_saved',side_effect=verify), \
             patch.object(matching.asyncio,'sleep',new_callable=AsyncMock),fence.owner_scope(self.lease):
            await matching.save_receipt(self.app,'fixture',{}, {},{'financial_saves_this_run':0},persist,self.frame,None,None,None)
    async def test_manual_save_is_retired_without_admission_or_click(self):
        with self.assertRaisesRegex(ValueError, 'icepay_manual_matching_disabled'):
            await self.save()
        self.assertEqual(self.events, [])
        self.frame.locator.return_value.click.assert_not_awaited()
        self.assertFalse(self.lease.lost.is_set())
