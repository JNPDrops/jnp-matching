import copy
from collections import Counter
import unittest
from operations import icepay_matching as m
from operations.test_icepay_apply import fixture


def example():
    manifest, ledger = fixture()
    for row in ledger:
        row.update(EntryID='entry-' + str(row['EntryNumber']), Currency='EUR')
    receipt = m.source_receipts(manifest, ledger)[0]
    invoice = {'YourRef': receipt['source_order'], 'JournalCode': '70', 'GLAccountCode': '1100',
        'AmountDC': receipt['amount'], 'Account': m.DEBTOR, 'AccountCode': '109419',
        'Currency': 'EUR', 'EntryNumber': 26700001}
    debit = {'YourRef': receipt['source_order'], 'JournalCode': '70', 'InvoiceNumber': 26700001,
        'AccountId': m.DEBTOR, 'AccountCode': '109419'.rjust(18), 'CurrencyCode': 'EUR',
        'Amount': receipt['amount'], 'AmountInTransit': 0}
    credit = {**debit, 'JournalCode': '27', 'Description': receipt['description'],
        'InvoiceNumber': receipt['entry'], 'Amount': str(-m.money(receipt['amount']))}
    return receipt, [invoice], [debit, credit], Counter({receipt['source_order']: 1})


class OwnOrderGuards(unittest.TestCase):
    def test_exact_own_full_invoice_and_receipt(self):
        receipt, history, opened, counts = example()
        self.assertIsNone(m.classify(receipt, history, opened, counts))

    def test_equal_amount_different_order_never_matches(self):
        receipt, history, opened, counts = example()
        history[0]['YourRef'] = 'TD999999'
        self.assertEqual(m.classify(receipt, history, opened, counts), 'invoice_not_found_including_closed_and_other_debtors')

    def test_other_debtor_is_found_but_never_moved(self):
        receipt, history, opened, counts = example()
        history[0]['AccountCode'] = '100100'
        self.assertEqual(m.classify(receipt, history, opened, counts), 'invoice_on_other_debtor')
        self.assertIsNotNone(receipt['invoice'])

    def test_closed_partial_currency_and_transit_block(self):
        receipt, history, opened, counts = example()
        self.assertEqual(m.classify(receipt, history, opened[1:], counts), 'invoice_closed_or_receipt_not_fully_open')
        for key, value, reason in [('Amount', '0.01', 'partial_amount_or_in_transit'),
            ('CurrencyCode', 'USD', 'open_item_identity_changed'), ('AmountInTransit', '1.00', 'partial_amount_or_in_transit')]:
            changed = copy.deepcopy(opened)
            changed[0][key] = value
            self.assertEqual(m.classify(receipt, history, changed, counts), reason)

    def test_differences_duplicates_and_wrong_receipt_reference_block(self):
        receipt, history, opened, counts = example()
        history[0]['AmountDC'] = '0.01'
        self.assertEqual(m.classify(receipt, history, opened, counts), 'amount_difference_requires_case_decision')
        receipt, history, opened, counts = example()
        self.assertEqual(m.classify(receipt, history*2, opened, counts), 'multiple_invoices_for_order')
        counts[receipt['source_order']] = 2
        self.assertEqual(m.classify(receipt, history, opened, counts), 'multiple_source_receipts_for_order')
        counts[receipt['source_order']] = 1
        opened[1]['YourRef'] = 'TD999999'
        self.assertEqual(m.classify(receipt, history, opened, counts), 'receipt_reference_changed')

    def test_changed_source_identity_is_rejected(self):
        manifest, ledger = fixture()
        for row in ledger:
            row.update(EntryID='entry-' + str(row['EntryNumber']), Currency='EUR')
        ledger[0]['EntryID'] = 'another-entry'
        with self.assertRaisesRegex(ValueError, 'source_identity_changed'):
            m.source_receipts(manifest, ledger)


if __name__ == '__main__':
    unittest.main()
