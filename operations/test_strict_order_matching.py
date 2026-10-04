import unittest
from copy import deepcopy
from fastapi import HTTPException
from operations.strict_order_matching import own_invoice, source_receipts, verify_wrong_selection, verify_source


class StrictMatchingTests(unittest.TestCase):
    def setUp(self):
        self.receipt = dict(source_order='TD100', amount='25.00', allocated_reference='TD200',
            bank_line_id='bank', offset_id='offset', description='Order TD100 | Woo 900 | Betaling ABcd1234')
        self.invoice = dict(ID='invoice', EntryNumber=1001, YourRef='TD100', AmountDC=25,
                            GLAccountCode='1100', AccountCode='customer', JournalCode='70')
        self.row = dict(id='List_row_0', checked=True, amount='25,00', matchId='match', writeoff='0',
                        cells=['', 'customer', '1002', '', 'TD200', '70 - Sales', '25,00', '', '', ''])

    def test_equal_amount_other_order_is_never_a_candidate(self):
        other = {**self.invoice, 'YourRef':'TD200'}
        self.assertEqual(own_invoice(self.receipt, [other], 'customer')[1], 'invoice_missing_or_ambiguous')
        selected, reason = own_invoice(self.receipt, [other, self.invoice], 'customer')
        self.assertIs(selected, self.invoice)
        self.assertIsNone(reason)

    def test_duplicate_invoice_identity_stops(self):
        self.assertIsNone(own_invoice(self.receipt, [self.invoice, {**self.invoice, 'ID':'other'}], 'customer')[0])

    def test_different_debtor_or_amount_needs_exception(self):
        for patch in [{'AccountCode':'other'}, {'AmountDC':24.99}]:
            self.assertIsNone(own_invoice(self.receipt, [{**self.invoice, **patch}], 'customer')[0])

    def test_wrong_match_requires_actual_checked_invoice(self):
        self.assertEqual(verify_wrong_selection(self.receipt, [self.row])['matchId'], 'match')
        for patch in [{'checked':False}, {'matchId':''}, {'writeoff':'3'}, {'amount':'24,99'}]:
            with self.assertRaises(HTTPException):
                verify_wrong_selection(self.receipt, [{**self.row, **patch}])
        correct = deepcopy(self.row)
        correct['cells'][4] = 'TD100'
        with self.assertRaises(HTTPException):
            verify_wrong_selection(self.receipt, [correct])

    def test_mixed_or_partial_matches_are_not_undone(self):
        with self.assertRaises(HTTPException):
            verify_wrong_selection(self.receipt, [self.row, self.row])
        partial = deepcopy(self.row)
        partial['cells'][6] = '20,00'
        with self.assertRaises(HTTPException):
            verify_wrong_selection(self.receipt, [partial])

    def test_original_receipt_amount_is_preserved(self):
        lines = [dict(ID='bank', AmountDC=25, Description=self.receipt['description']),
                 dict(ID='offset', AmountDC=-25, Description=self.receipt['description'])]
        verify_source(self.receipt, lines)
        lines[0]['AmountDC'] = 24
        with self.assertRaises(HTTPException):
            verify_source(self.receipt, lines)

    def test_sources_require_unique_offset_and_transaction(self):
        base = dict(EntryID='entry', EntryNumber=1, Description=self.receipt['description'], AccountCode='customer', YourRef='TD200')
        bank = dict(base, ID='bank', AmountDC=25, GLAccountCode='bank')
        offset = dict(base, ID='offset', AmountDC=-25, GLAccountCode='1100')
        found = source_receipts([bank, offset], 'customer')
        self.assertEqual(found[0]['source_order'], 'TD100')
        for lines in [[bank], [bank, offset, {**offset, 'ID':'duplicate'}], [bank, bank, offset]]:
            with self.assertRaises(HTTPException):
                source_receipts(lines, 'customer')


if __name__ == '__main__':
    unittest.main()
