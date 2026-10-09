import unittest

from forge.factory.control import attention_kind
from forge.factory.ledger import APPROACH_HOLD


class AttentionKindTests(unittest.TestCase):
    def test_each_reason_has_one_code(self):
        cases = [
            ({'state': 'approval_needed', 'merge_gate': {'kind': 'approval'}}, 'approve'),
            ({'state': 'approval_needed', 'merge_gate': {'kind': 'blocked'}}, 'review'),
            ({'state': 'approval_needed', 'merge_gate': {'kind': 'approval'},
              'evidence': {'audit': {'human_required': 'required', 'human_reason': 'Build the image and check the PDF'}}}, 'verify'),
            ({'state': 'failed'}, 'failed'),
            ({'state': 'hold', 'decision_request': {'question': 'Wait for #87?'}}, 'question'),
            ({'state': 'hold', 'reason': APPROACH_HOLD}, 'approach'),
            ({'state': 'hold', 'environment_hold': {'blocker': 'sandbox'}}, 'env'),
            ({'state': 'hold', 'prerequisite_hold': {'state': 'ready'}}, 'env'),
            ({'state': 'hold', 'operator_hold': {'kind': 'conflict'}}, 'review'),
            ({'state': 'hold', 'operator_hold': {'kind': 'pause'}}, None),
            ({'state': 'hold', 'reason': APPROACH_HOLD, 'blocked_dependencies': [90]}, None),
            ({'state': 'hold', 'reason': 'Waiting for dry-run evidence', 'label_hold': ['hold']}, None),
            ({'state': 'hold', 'reason': 'Waiting for dry-run evidence'}, 'review'),
            ({'state': 'ready'}, None),
            ({'state': 'merged'}, None),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(attention_kind(item), expected)


if __name__ == '__main__':
    unittest.main()
