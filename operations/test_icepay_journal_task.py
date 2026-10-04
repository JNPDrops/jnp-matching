import unittest
import copy
from types import SimpleNamespace
from unittest.mock import patch

from operations import icepay_journal_task as task


def ledgers():
    return [dict(ID=task.LEDGER_ID, Code='1317', Description='Icepay EUR', Type=12,
                 BalanceType='B', IsBlocked=False),
            dict(ID=task.UNALLOCATED_ID, Code='1360', Type=90,
                 BalanceType='B', IsBlocked=False)]


def reference():
    return dict(Code='26', Description='Fibonatix EUR', Type=12, Currency='EUR',
                PaymentInTransitAccount=task.UNALLOCATED_ID)


class FakeAPI:
    def __init__(self, journals=None, fail=False):
        self.journals = journals if journals is not None else [reference()]
        self.posts = []
        self.audit = []
        self.fail = fail
    async def rows(self, resource, params=None):
        if resource == 'financial/Journals':
            return copy.deepcopy(self.journals)
        if resource == 'financial/GLAccounts':
            return ledgers()
        if resource == 'crm/BankAccounts':
            return []
        raise AssertionError(resource)
    async def persist(self, value):
        self.audit.append(value)
    async def create_journal(self, payload):
        assert self.audit[-1]['status'] == 'write_started'
        self.posts.append(payload)
        if self.fail:
            raise TimeoutError('uncertain outcome')
        self.journals.append({**payload, 'BankAccountCountry':'NL ', 'BankAccountID':'new-bank'})


class Boundaries(unittest.TestCase):
    def test_wrong_division_or_host_stops_before_api(self):
        for division, host in [(1, task.BASE), (task.DIVISION, 'https://elsewhere.test')]:
            with self.assertRaises(task.Stopped):
                task.Reader(SimpleNamespace(DIVISION=division, BASE_URL=host))

    def test_pagination_cannot_leave_resource_or_origin(self):
        root = task.BASE + '/api/v1/3977752/financial/Journals'
        self.assertTrue(task.allowed_url(root+'?$skiptoken=20', 'financial/Journals'))
        for url in [root.replace('3977752', '1'), root.replace('Journals', 'GLAccounts'),
                    root.replace('https:', 'http:'), root+'#x',
                    root.replace('start.exactonline.nl', 'evil.test'),
                    root.replace('start.exactonline.nl', 'user@start.exactonline.nl')]:
            self.assertFalse(task.allowed_url(url, 'financial/Journals'))


class ReadOnly(unittest.IsolatedAsyncioTestCase):
    async def test_reads_inventory_and_does_not_offer_write_method(self):
        calls = []
        class FakeReader:
            def __init__(self, app):
                self.calls = 0
            async def rows(self, resource):
                calls.append(resource)
                self.calls += 1
                return [{'Code':'26', 'Description':'Fibonatix EUR'},
                        {'Code':'27', 'Description':'ICEPAY EUR'}] if resource == 'financial/Journals' else []
        with patch.object(task, 'Reader', FakeReader), patch.object(task, 'event'):
            result = await task.inspect(None)
        self.assertEqual(result['icepay_journal_count'], 1)
        self.assertEqual(result['exact_writes'], 0)
        self.assertEqual(calls, ['financial/Journals', 'financial/GLAccounts'])

    async def test_existing_claim_does_not_repeat_calls(self):
        with patch.object(task, 'claim', return_value=False), patch.object(task, 'inspect') as inspect:
            await task.run(None)
            inspect.assert_not_called()


class Creation(unittest.IsolatedAsyncioTestCase):
    async def test_one_post_audited_before_request_and_verified_after(self):
        api = FakeAPI()
        result = await task.create_once(None, api, api.persist)
        self.assertEqual(result['status'], 'created_verified')
        self.assertEqual(api.posts, [task.PAYLOAD])

    async def test_existing_bank_journal_does_not_write(self):
        existing = {**task.PAYLOAD, 'BankAccountID':'existing-bank'}
        api = FakeAPI([reference(), existing])
        result = await task.create_once(None, api, api.persist)
        self.assertEqual(result['status'], 'already_exists')
        self.assertEqual(api.posts, [])

    async def test_taken_code_stops_without_write(self):
        api = FakeAPI([reference(), dict(Code='27', Description='Other bank')])
        with self.assertRaises(task.Stopped):
            await task.create_once(None, api, api.persist)
        self.assertEqual(api.posts, [])

    async def test_wrong_existing_configuration_is_not_overwritten(self):
        existing = {**task.PAYLOAD, 'Currency':'USD', 'BankAccountID':'existing-bank'}
        api = FakeAPI([reference(), existing])
        with self.assertRaises(task.Stopped):
            await task.create_once(None, api, api.persist)
        self.assertEqual(api.posts, [])

    async def test_timeout_is_not_retried(self):
        api = FakeAPI(fail=True)
        with self.assertRaises(TimeoutError):
            await task.create_once(None, api, api.persist)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.audit[-1]['exact_writes'], 'unknown')

    async def test_activation_required(self):
        with patch.dict(task.os.environ, {}, clear=True), patch.object(task, 'claim') as claim:
            await task.run_create(None)
            claim.assert_not_called()

    async def test_racing_journal_is_found_before_post(self):
        api = FakeAPI()
        original = api.rows
        reads = 0
        async def rows(resource, params=None):
            nonlocal reads
            if resource == 'financial/Journals':
                reads += 1
                if reads == 2:
                    api.journals.append({**task.PAYLOAD, 'BankAccountID':'concurrent-bank'})
            return await original(resource, params)
        api.rows = rows
        result = await task.create_once(None, api, api.persist)
        self.assertEqual(result['status'], 'already_exists')
        self.assertEqual(api.posts, [])


if __name__ == '__main__':
    unittest.main()
