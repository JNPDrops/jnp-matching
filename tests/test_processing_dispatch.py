import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from operations import processing_dispatch as dispatch


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app=SimpleNamespace(DIVISION=3977752)
        self.job={'task_key':'jnp:3977752:batch:2026-10-10T0900','job_id':'synthetic-job',
            'action':'processing_stage','params':{'stage':'fibonatix_source'}}

    async def test_missing_adapter_never_completes_as_success(self):
        with patch.object(dispatch,'validate',return_value='fibonatix_source'),patch.object(dispatch,'save_outcome') as save:
            with self.assertRaisesRegex(ValueError,'adapter_not_installed'):
                await dispatch.run(self.app,self.job)
        self.assertEqual(save.call_args.args[3],'blocked')
        self.assertEqual(save.call_args.args[4]['reason'],'processing_stage_adapter_not_installed')

    async def test_write_intent_failure_is_uncertain_and_never_retried(self):
        adapter=AsyncMock(side_effect=TimeoutError('synthetic timeout'))
        with patch.object(dispatch,'validate',return_value='fibonatix_import'), \
             patch.dict(dispatch.ADAPTERS,{'fibonatix_import':adapter}), \
             patch.object(dispatch,'import_may_have_written',return_value=True), \
             patch.object(dispatch,'save_outcome') as save:
            with self.assertRaises(TimeoutError):await dispatch.run(self.app,self.job)
        self.assertEqual(adapter.await_count,1)
        self.assertEqual(save.call_args.args[3],'uncertain')

    async def test_registry_coverage_requires_every_stage(self):
        with patch.dict(dispatch.ADAPTERS,{},clear=True):
            self.assertEqual(dispatch.missing_adapters(),[s for s,_ in dispatch.coordinator.STAGES])

    async def test_nonverified_provider_result_is_not_success(self):
        adapter=AsyncMock(return_value={'state':'import_requested'})
        with patch.object(dispatch,'validate',return_value='routing'), \
             patch.dict(dispatch.ADAPTERS,{'routing':adapter}), \
             patch.object(dispatch,'save_outcome') as save:
            with self.assertRaisesRegex(ValueError,'not_verified'):await dispatch.run(self.app,self.job)
        self.assertEqual(save.call_args.args[3],'blocked')
