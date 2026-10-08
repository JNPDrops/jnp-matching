import unittest
from unittest.mock import AsyncMock, patch
from operations import icepay_source_window as s

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_installer_gets_only_supplied_environment(self):
        child=AsyncMock(); child.wait.return_value=0
        with patch.object(s.asyncio,'create_subprocess_exec',AsyncMock(return_value=child)) as create, patch.object(s,'stop_child',AsyncMock()) as stop:
            await s.ensure_browser({'PATH':'safe','PLAYWRIGHT_BROWSERS_PATH':'pinned'})
        args,kwargs=create.call_args
        self.assertEqual(args[1:],('-m','playwright','install','chromium','--only-shell'))
        self.assertEqual(kwargs['env'],{'PATH':'safe','PLAYWRIGHT_BROWSERS_PATH':'pinned'})
        stop.assert_awaited_once_with(child)
    async def test_failed_install_stops_before_capture(self):
        child=AsyncMock(); child.wait.return_value=1
        with patch.object(s.asyncio,'create_subprocess_exec',AsyncMock(return_value=child)), patch.object(s,'stop_child',AsyncMock()) as stop:
            with self.assertRaisesRegex(s.t.AcquisitionStopped,'browser_install'):
                await s.ensure_browser({})
        stop.assert_awaited_once_with(child)

if __name__=='__main__': unittest.main()
