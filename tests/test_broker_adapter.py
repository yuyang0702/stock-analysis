from __future__ import annotations

import unittest

from broker_adapter import (
    BrokerAdapterUnavailable,
    ManualAdapter,
    SubmissionReceipt,
)
from execution_contracts import ExecutionIntent
from tests.test_execution_contracts import intent_values, make_candidate


class BrokerAdapterTest(unittest.TestCase):
    def test_manual_adapter_never_claims_broker_submission(self) -> None:
        intent = ExecutionIntent(**intent_values(make_candidate()))
        receipt = ManualAdapter().submit_intent(intent)

        self.assertEqual(receipt.status, "manual_confirmation_required")
        self.assertFalse(receipt.broker_request_made)
        self.assertIsNone(receipt.broker_order_id)
        self.assertTrue(ManualAdapter().capabilities()["manual_ticket"])

    def test_manual_adapter_refuses_snapshot_and_cancel(self) -> None:
        adapter = ManualAdapter()
        with self.assertRaises(BrokerAdapterUnavailable):
            adapter.get_snapshot(account_scope_id="paper")
        with self.assertRaises(BrokerAdapterUnavailable):
            adapter.cancel_order(broker_order_id="broker-1")

    def test_submission_receipt_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported submission status"):
            SubmissionReceipt(
                adapter="paper",
                client_order_id="client-1",
                status="filled",
            )

    def test_submission_receipt_does_not_allow_ambiguous_empty_rejection(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a message"):
            SubmissionReceipt(
                adapter="qmt", client_order_id="client-1", status="unknown"
            )
        with self.assertRaisesRegex(ValueError, "manual receipt"):
            SubmissionReceipt(
                adapter="manual", client_order_id="client-1",
                status="manual_confirmation_required", broker_order_id="broker-1",
            )


if __name__ == "__main__":
    unittest.main()
