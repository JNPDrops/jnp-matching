import csv
import io
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from operations.fibonatix_daily_source import bounds, prepare, read_source


class DailySourceTests(unittest.TestCase):
    def fixture(self):
        rows = []
        for i, stamp in enumerate(['2026-10-05 21:59:59', '2026-10-05 22:00:00', '2026-10-06 21:59:59', '2026-10-06 22:00:00']):
            rows.append({'TRX ID': 'PAY000'+str(i), 'Display Time': stamp, 'Brand': 'TheDrops', 'Currency': 'EUR',
                         'Status(approved/declined)': 'Approved', 'Status Code': '20000', 'Type': 'SL',
                         'Brand TRX ID': str(10+i), 'Order Description': 'Order #'+str(10+i), 'Amount': '25.00'})
        return rows

    def encoded(self, rows):
        stream = io.StringIO();writer = csv.DictWriter(stream,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)
        return stream.getvalue().encode()

    def proof(self, rows):
        return [{'trx': r['TRX ID'], 'amsterdam': datetime.strptime(r['Display Time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Amsterdam')).isoformat()} for r in rows]

    def test_exact_calendar_edges(self):
        rows=self.fixture();got,summary=read_source(self.encoded(rows),date(2026,10,6),utc_ui_proof=self.proof(rows))
        self.assertEqual([r['TRX ID'] for r in got],['PAY0001','PAY0002']);self.assertEqual(summary['outside_rows'],2)

    def test_dst_days_are_not_fixed_24_hours(self):
        for day,hours in [(date(2026,3,29),23),(date(2026,10,25),25)]:
            lo,hi=bounds(day);self.assertEqual((hi-lo).total_seconds()/3600,hours)

    def test_wrong_clock_fails_closed(self):
        rows=self.fixture();proof=self.proof(rows);proof[0]['amsterdam']='2026-10-06T01:59:59+02:00'
        with self.assertRaisesRegex(ValueError,'timezone_proof_disagrees'):read_source(self.encoded(rows),date(2026,10,6),utc_ui_proof=proof)

    def test_paid_processing_order_and_own_order_number(self):
        rows=self.fixture();orders=[{'order_id':11,'order_number':9001,'payment_method':'wc_fibonatix','currency':'EUR','status':'completed','total':'25.00','total_refunds':'0'}, {'order_id':12,'order_number':9002,'payment_method':'wc_fibonatix','currency':'EUR','status':'processing','total':'25.00','total_refunds':'0'}]
        got,exc,_=prepare(self.encoded(rows),orders,date(2026,10,6),utc_ui_proof=self.proof(rows))
        self.assertEqual(got[0]['ref'],'TD9001');self.assertEqual(len(got),2);self.assertEqual(exc,[])

    def test_later_order_refund_does_not_erase_successful_original_payment(self):
        rows=self.fixture()
        for status in ['processing','pending','on-hold','cancelled','refunded','failed']:
            orders=[{'order_id':11,'order_number':9001,'payment_method':'wc_fibonatix','currency':'EUR','status':status,'total':'25.00','total_refunds':'25.00'}, {'order_id':12,'order_number':9002,'payment_method':'wc_fibonatix','currency':'EUR','status':status,'total':'25.00','total_refunds':'0'}]
            got,exc,_=prepare(self.encoded(rows),orders,date(2026,10,6),utc_ui_proof=self.proof(rows))
            self.assertEqual(len(got),2,status);self.assertEqual(exc,[])

    def test_refund_and_failed_never_positive_receipts(self):
        rows=self.fixture();rows[1]['Type']='RF';rows[2]['Status(approved/declined)']='Declined'
        got,exc,_=prepare(self.encoded(rows),[],date(2026,10,6),utc_ui_proof=self.proof(rows))
        self.assertFalse(got);self.assertEqual({x['reason'] for x in exc},{'separate_policy_RF','source_declined'})

    def test_duplicate_psp_id_rejected(self):
        rows=self.fixture();rows[1]['TRX ID']=rows[0]['TRX ID']
        with self.assertRaisesRegex(ValueError,'duplicate_psp_id'):read_source(self.encoded(rows),date(2026,10,6),utc_ui_proof=self.proof(rows))


if __name__=='__main__':unittest.main()
