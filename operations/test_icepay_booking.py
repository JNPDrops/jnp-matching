import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from operations import icepay_booking as b

TEMPLATE=b'''<eExact><GLTransactions><GLTransaction entry="26260008">
<TransactionType number="20"/><Journal code="26"/><Date>2026-09-22</Date>
<FinYear number="2026"/><FinPeriod number="9"/><Description>old batch</Description>
<GLTransactionLine line="1" linetype="0" offsetline="0" status="20" type="40">
<Date>2026-09-22</Date><VATType>S</VATType><FinYear number="2026"/><FinPeriod number="9"/>
<GLAccount code="1100"/><Description>old payment</Description><Account code="100100"/>
<Amount><Currency code="EUR"/><Value>1.00</Value></Amount>
<References><PaymentReference>OLD12345</PaymentReference><YourRef>TD11111</YourRef></References>
<Note>old note</Note></GLTransactionLine></GLTransaction></GLTransactions></eExact>'''


def receipts():
    result=[]
    for day,(count,total) in b.DAY_TOTALS.items():
        for i in range(count):
            result.append({'payment_id':str(88700000+len(result)),'merchant':'34950','status':'OK',
                'order':str(49000+len(result)),'date':day,
                'amount':str(Decimal('1.00') if i<count-1 else total-(count-1))})
    return result


class Batch(unittest.TestCase):
    def test_three_entries_preserve_source_ids_orders_dates_and_total(self):
        source=receipts(); xml,manifest,metadata=b.build_xml(source,TEMPLATE)
        root=ET.fromstring(xml); entries=root.findall('./GLTransactions/GLTransaction')
        self.assertEqual([e.get('entry') for e in entries],['26270001','26270002','26270003'])
        self.assertEqual(len(manifest),36)
        self.assertEqual(sum(Decimal(r['amount']) for r in manifest),Decimal('3161.24'))
        for entry in entries:
            self.assertEqual(entry.find('Journal').get('code'),'27')
            for line in entry.findall('GLTransactionLine'):
                row=next(r for r in manifest if r['payment_id']==line.findtext('References/PaymentReference'))
                self.assertEqual(line.findtext('References/YourRef'),'TD'+row['order'])
                self.assertEqual(line.findtext('Date'),row['date'])
                self.assertEqual(line.findtext('Amount/Value'),row['amount'])
                self.assertEqual(line.find('Account').get('code'),'109419')
                self.assertEqual(line.find('GLAccount').get('code'),'1100')
                self.assertIn(b.JOB,line.findtext('Note'))
        self.assertNotIn(b'OLD12345',xml); self.assertNotIn(b'old note',xml)

    def test_failed_duplicate_changed_amount_or_date_never_build(self):
        for key,value in [('status','ERR'),('date','2026-09-30'),('amount','0.00'),('amount','1.01'),('merchant','1'),('order',None)]:
            rows=receipts(); rows[0][key]=value
            with self.assertRaises(ValueError): b.build_xml(rows,TEMPLATE)
        rows=receipts(); rows[0]['payment_id']=rows[1]['payment_id']
        with self.assertRaises(ValueError): b.build_xml(rows,TEMPLATE)

    def test_unknown_template_field_cannot_leak_into_new_bookings(self):
        altered=TEMPLATE.replace(b'<Note>',b'<Unexpected>old reference</Unexpected><Note>')
        with self.assertRaises(ValueError): b.build_xml(receipts(),altered)


if __name__=='__main__': unittest.main()
