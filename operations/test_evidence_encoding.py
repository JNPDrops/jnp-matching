import base64
import unittest
from copy import deepcopy
from unittest.mock import patch
from operations import fibonatix_import as m


class EvidenceEncodingTests(unittest.TestCase):
    def test_plain_existing_artifacts_remain_readable(self):
        for value in [None, [], {'receipts': [{'amount': '19.99', 'attempts': []}]}]:
            self.assertIs(m.decode_evidence(value), value)

    def test_complete_roundtrip_and_deterministic_encoding(self):
        value = {'order': 'synthetic', 'unicode': 'betaling € café', 'empty': None,
                 'rows': [{'id': str(i), 'amount': 19.99, 'checked': True} for i in range(1000)]}
        packed = m.encode_evidence(value)
        self.assertEqual(m.decode_evidence(packed), value)
        self.assertEqual(m.encode_evidence(value), packed)
        self.assertLess(len(packed['payload']), packed['json_bytes'])

    def test_corrupted_checksum_or_declared_size_is_rejected(self):
        original = m.encode_evidence({'proof': [1, 2, 3]})
        for key, value in [('sha256', '0' * 64), ('json_bytes', original['json_bytes'] + 1)]:
            corrupted = deepcopy(original)
            corrupted[key] = value
            with self.assertRaises(ValueError):
                m.decode_evidence(corrupted)

    def test_unsupported_or_oversized_envelope_is_rejected(self):
        original = m.encode_evidence({'proof': []})
        for key, value in [('__private_evidence__', 'unknown'), ('json_bytes', m.MAX_EVIDENCE_BYTES + 1)]:
            corrupted = deepcopy(original)
            corrupted[key] = value
            with self.assertRaises(ValueError):
                m.decode_evidence(corrupted)
        with patch.object(m, 'MAX_EVIDENCE_BYTES', 2):
            with self.assertRaises(ValueError):
                m.encode_evidence({'proof': [1]})

    def test_malformed_payload_is_rejected(self):
        packed = m.encode_evidence({'proof': []})
        packed['payload'] = base64.b64encode(b'not a gzip stream').decode()
        with self.assertRaises(OSError):
            m.decode_evidence(packed)


if __name__ == '__main__':
    unittest.main()
