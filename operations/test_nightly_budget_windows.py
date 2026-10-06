import unittest
from operations.worker_coordination import available


class BudgetWindows(unittest.TestCase):
    def setUp(self):
        self.state = dict(daily_remaining=2500,daily_reset_ms=100000,
                          minute_remaining=59,minute_reset_ms=100000)

    def test_old_uncertainty_keeps_daily_reserve_without_freezing_minute(self):
        self.assertEqual(available(self.state,59,floor=200,now_ms=1000,pending_minute=0),(True,None))
        self.state['daily_remaining']=259
        self.assertEqual(available(self.state,59,floor=200,now_ms=1000,pending_minute=0),(False,'daily_reserve'))

    def test_active_reservations_still_exhaust_minute(self):
        self.assertEqual(available(self.state,59,floor=200,now_ms=1000,pending_minute=59),(False,'minute_reserve'))

    def test_default_retains_conservative_existing_callers(self):
        self.assertEqual(available(self.state,59,floor=200,now_ms=1000),(False,'minute_reserve'))

    def test_unknown_quota_stays_capped(self):
        self.state['minute_reset_ms']=500
        self.assertEqual(available(self.state,59,floor=200,now_ms=1000,pending_minute=0,unknown_count=30),(False,'unknown_minute_cap'))


if __name__=='__main__':unittest.main()
