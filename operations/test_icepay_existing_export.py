import sys,types,unittest
from unittest.mock import AsyncMock,MagicMock,patch
from operations import icepay_existing_export as m

class CollectTests(unittest.IsolatedAsyncioTestCase):
 async def test_not_ready_notifications_never_submit_export_or_call_exact(self):
  page=MagicMock();page.get_by_role.return_value=MagicMock()
  context=MagicMock();context.new_page=AsyncMock(return_value=page)
  browser=MagicMock();browser.new_context=AsyncMock(return_value=context);browser.close=AsyncMock()
  manager=MagicMock();runtime=MagicMock();runtime.chromium.launch=AsyncMock(return_value=browser)
  manager.__aenter__=AsyncMock(return_value=runtime);manager.__aexit__=AsyncMock(return_value=False)
  module=types.ModuleType('playwright.async_api');module.async_playwright=lambda:manager
  parent=types.ModuleType('playwright');parent.async_api=module
  with patch.dict(sys.modules,{'playwright':parent,'playwright.async_api':module}),patch.object(m.b,'protect_requests',AsyncMock()),patch.object(m.b,'authenticate',AsyncMock(return_value={'account_verified':True})),patch.object(m.b.Credentials,'from_env',return_value=object()),patch.object(m.t,'open_notifications',AsyncMock(side_effect=TimeoutError)),patch.object(m.t,'payment_export',AsyncMock()) as modern,patch.object(m.t,'legacy_payment_export',AsyncMock()) as legacy:
   status,artifacts,summary=await m.collect({'proof':{'period_from':'2026-10-04','period_through':'2026-10-05','ui_payment_ids':['1']}})
  self.assertEqual(status['stage'],'open_notifications');self.assertEqual(status['state'],'blocked')
  modern.assert_not_called();legacy.assert_not_called();browser.close.assert_awaited_once()
  self.assertFalse(status['financial_writes'])

if __name__=='__main__':unittest.main()
