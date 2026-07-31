from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .daraja import DarajaError, normalize_phone
from .models import Transaction
from .views import expire_stale_pending


def callback_payload(checkout_id, result_code=0, receipt="SGH4X9K2LM", amount=1500, merchant="29115-1"):
    stk = {
        "MerchantRequestID": merchant,
        "CheckoutRequestID": checkout_id,
        "ResultCode": result_code,
        "ResultDesc": "The service request is processed successfully." if result_code == 0 else "Request cancelled by user",
    }
    if result_code == 0:
        stk["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": 20260731200500},
                {"Name": "PhoneNumber", "Value": 254712345678},
            ]
        }
    return {"Body": {"stkCallback": stk}}


class PhoneNormalisationTests(TestCase):
    def test_accepts_every_format_a_kenyan_user_might_type(self):
        for raw in ["0712345678", "254712345678", "+254712345678", "712345678", "+254 712 345 678"]:
            self.assertEqual(normalize_phone(raw), "254712345678", msg=raw)

    def test_accepts_airtel_prefix(self):
        self.assertEqual(normalize_phone("0733555777"), "254733555777")
        self.assertEqual(normalize_phone("0101234567"), "254101234567")

    def test_rejects_nonsense(self):
        for raw in ["12345", "", "abcdefghij", "07123456789012"]:
            with self.assertRaises(DarajaError, msg=raw):
                normalize_phone(raw)


class CallbackTests(TestCase):
    def setUp(self):
        self.txn = Transaction.objects.create(
            checkout_request_id="ws_CO_1",
            merchant_request_id="29115-1",
            phone="254712345678",
            amount=1500,
            account_reference="INV-4471",
            description="Demo",
        )

    def post_callback(self, payload):
        return self.client.post("/api/mpesa/callback/", payload, content_type="application/json")

    def test_success_settles_the_transaction(self):
        response = self.post_callback(callback_payload("ws_CO_1"))
        self.assertEqual(response.status_code, 200)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, Transaction.Status.SUCCESS)
        self.assertEqual(self.txn.mpesa_receipt, "SGH4X9K2LM")
        self.assertIsNotNone(self.txn.raw_callback)

    def test_cancellation_is_distinguished_from_failure(self):
        self.post_callback(callback_payload("ws_CO_1", result_code=1032))
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, Transaction.Status.CANCELLED)

    def test_duplicate_callback_does_not_overwrite(self):
        """Safaricom retries. The second delivery must change nothing."""
        self.post_callback(callback_payload("ws_CO_1"))
        response = self.post_callback(callback_payload("ws_CO_1", receipt="DIFFERENT"))

        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.mpesa_receipt, "SGH4X9K2LM")

    def test_orphan_callback_is_acknowledged_not_errored(self):
        response = self.post_callback(callback_payload("ws_CO_NOT_OURS"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ResultCode"], 0)

    def test_malformed_payload_is_acknowledged(self):
        """Anything other than a 200 makes Daraja retry, which turns our bug into a storm."""
        response = self.client.post("/api/mpesa/callback/", {"garbage": True}, content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def test_receipt_is_unique_at_the_database_level(self):
        self.post_callback(callback_payload("ws_CO_1"))

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Transaction.objects.create(
                    checkout_request_id="ws_CO_2",
                    merchant_request_id="29115-2",
                    phone="254700000000",
                    amount=10,
                    account_reference="X",
                    description="d",
                    status=Transaction.Status.SUCCESS,
                    mpesa_receipt="SGH4X9K2LM",
                )


class ReconciliationTests(TestCase):
    def test_summary_counts_and_totals(self):
        Transaction.objects.create(
            checkout_request_id="a", merchant_request_id="1", phone="254712345678", amount=1500,
            account_reference="A", description="d", status=Transaction.Status.SUCCESS, mpesa_receipt="R1",
        )
        Transaction.objects.create(
            checkout_request_id="b", merchant_request_id="2", phone="254712345679", amount=800,
            account_reference="B", description="d", status=Transaction.Status.CANCELLED,
        )
        Transaction.objects.create(
            checkout_request_id="c", merchant_request_id="3", phone="254712345670", amount=2500,
            account_reference="C", description="d", status=Transaction.Status.SUCCESS, mpesa_receipt=None,
        )

        summary = self.client.get("/api/transactions/", headers={"accept": "application/json"}).json()["summary"]

        self.assertEqual(summary["collected"], 4000)
        self.assertEqual(summary["successful"], 2)
        self.assertEqual(summary["failed"], 1)
        # Settled but with no receipt — the row a human would otherwise have to chase.
        self.assertEqual(summary["unreconciled"], 1)

    def test_abandoned_prompts_are_swept(self):
        txn = Transaction.objects.create(
            checkout_request_id="stale", merchant_request_id="1", phone="254712345678",
            amount=100, account_reference="A", description="d",
        )
        Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(minutes=10))

        self.assertEqual(expire_stale_pending(), 1)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.TIMEOUT)

    def test_recent_pending_is_not_swept(self):
        txn = Transaction.objects.create(
            checkout_request_id="fresh", merchant_request_id="1", phone="254712345678",
            amount=100, account_reference="A", description="d",
        )
        self.assertEqual(expire_stale_pending(), 0)
        txn.refresh_from_db()
        self.assertEqual(txn.status, Transaction.Status.PENDING)


class SimulationGateTests(TestCase):
    def setUp(self):
        self.txn = Transaction.objects.create(
            checkout_request_id="ws_CO_SIM", merchant_request_id="1", phone="254712345678",
            amount=1500, account_reference="A", description="d",
        )

    @override_settings(ALLOW_SIMULATION=False)
    def test_blocked_when_disabled(self):
        response = self.client.post(
            "/api/dev/simulate-callback/", {"id": "ws_CO_SIM"},
            content_type="application/json", headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(ALLOW_SIMULATION=True)
    def test_runs_through_the_real_handler_when_enabled(self):
        response = self.client.post(
            "/api/dev/simulate-callback/", {"id": "ws_CO_SIM", "outcome": "success"},
            content_type="application/json", headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200)

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, Transaction.Status.SUCCESS)
        self.assertTrue(self.txn.mpesa_receipt)


class PushValidationTests(TestCase):
    def post_push(self, body):
        return self.client.post("/api/stk/push/", body, content_type="application/json", headers={"accept": "application/json"})

    def test_rejects_bad_amount(self):
        self.assertEqual(self.post_push({"phone": "0712345678", "amount": 0}).status_code, 400)
        self.assertEqual(self.post_push({"phone": "0712345678", "amount": "abc"}).status_code, 400)

    def test_rejects_bad_phone_before_calling_daraja(self):
        response = self.post_push({"phone": "12345", "amount": 100})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PHONE")
