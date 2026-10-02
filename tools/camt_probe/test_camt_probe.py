import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from camt_probe import NS, VARIANTS, build_probe

N = {'c': NS}
BASE = dict(iban='NL91ABNA0417164300', opening_balance='123.45', statement_number=8001,
            booking_date='2026-10-02', run_id='20261002A', variant='control')

class ProbeTests(unittest.TestCase):
    def make(self, **changes):
        return ET.fromstring(build_probe(**{**BASE, **changes}))
    def test_every_variant_is_one_credit_and_balanced(self):
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                root = self.make(variant=variant)
                self.assertEqual(len(root.findall('.//c:Ntry', N)), 1)
                self.assertEqual(root.findtext('.//c:Ntry/c:Amt', namespaces=N), '0.01')
                self.assertEqual(root.findtext('.//c:Ntry/c:CdtDbtInd', namespaces=N), 'CRDT')
                values = [Decimal(x.text) for x in root.findall('.//c:Bal/c:Amt', N)]
                self.assertEqual(values[1] - values[0], Decimal('0.01'))
    def test_reference_is_only_in_selected_field(self):
        fields = {'structured':'.//c:CdtrRefInf/c:Ref',
                  'end_to_end':'.//c:Refs/c:EndToEndId',
                  'unstructured':'.//c:RmtInf/c:Ustrd'}
        for variant in VARIANTS:
            root = self.make(variant=variant)
            matches = [x for x in root.iter() if x.text == 'JNPTEST-MAP-20261002A']
            self.assertEqual(len(matches), 0 if variant == 'control' else 1)
            if variant != 'control':
                self.assertEqual(root.findtext(fields[variant], namespaces=N), 'JNPTEST-MAP-20261002A')
    def test_no_real_order_or_debtor_in_output(self):
        for variant in VARIANTS:
            data = build_probe(**{**BASE, 'variant':variant}).decode()
            self.assertNotIn('48451', data)
            self.assertNotIn('100100', data)
            self.assertNotIn('SCOR', data)
    def test_invalid_inputs_fail(self):
        for change in [{'iban':'NL00ABNA0417164300'}, {'amount':'78.60'}, {'amount':'0'},
                       {'amount':'NaN'}, {'amount':'0.001'}, {'opening_balance':'Infinity'},
                       {'variant':'unknown'}, {'run_id':'../bad'}, {'statement_number':0},
                       {'booking_date':'2026-02-30'}]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError): self.make(**change)
    def test_negative_balance_sign(self):
        root = self.make(opening_balance='-5.00')
        self.assertEqual([x.text for x in root.findall('.//c:Bal/c:CdtDbtInd',N)], ['DBIT','DBIT'])
        self.assertEqual([x.text for x in root.findall('.//c:Bal/c:Amt',N)], ['5.00','4.99'])
    def test_statement_ids_are_distinct(self):
        ids={self.make(variant=v).findtext('.//c:Stmt/c:Id',namespaces=N) for v in VARIANTS}
        self.assertEqual(len(ids),len(VARIANTS))

if __name__ == '__main__': unittest.main()
