import unittest
from types import SimpleNamespace
from unittest.mock import patch

from operations import icepay_journal_task as task


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


if __name__ == '__main__':
    unittest.main()
