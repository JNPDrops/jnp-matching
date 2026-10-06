import unittest
from operations.paragon_source_discovery import safe_links

class NavigationTests(unittest.TestCase):
    def test_only_observed_read_navigation_on_exact_origin(self):
        self.assertEqual(safe_links([
            {'label':'Transactions','href':'https://paragon.online/transactions'},
            {'label':'Payments','href':'https://paragon.online.evil.example/payments'},
            {'label':'Refund','href':'https://paragon.online/refund'},
            {'label':'Reports','href':'https://paragon.online/reports?token=redacted'},
        ]),[{'label':'Transactions','path':'/transactions'}])

if __name__=='__main__': unittest.main()
