import logging
import random
import string
from datetime import timedelta

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from .daraja import DarajaError, initiate_stk_push, nairobi_timestamp, normalize_phone, query_stk_status
from .models import Transaction

logger = logging.getLogger("payments")

PENDING_TIMEOUT = timedelta(minutes=3)

# Daraja result codes worth distinguishing: 1032 is the customer pressing cancel,
# which is "they said no" rather than "something broke".
RESULT_STATUS = {0: Transaction.Status.SUCCESS, 1032: Transaction.Status.CANCELLED, 1037: Transaction.Status.TIMEOUT}


def status_for(result_code: int) -> str:
    return RESULT_STATUS.get(result_code, Transaction.Status.FAILED)


def expire_stale_pending() -> int:
    """A customer who ignores the prompt produces no callback at all. Without this
    the row sits at pending forever and quietly corrupts the day's totals."""
    cutoff = timezone.now() - PENDING_TIMEOUT
    return Transaction.objects.filter(status=Transaction.Status.PENDING, created_at__lt=cutoff).update(
        status=Transaction.Status.TIMEOUT,
        result_desc="No response from Daraja within the timeout window.",
    )


def dashboard(request):
    return render(
        request,
        "payments/dashboard.html",
        {"simulation_enabled": settings.ALLOW_SIMULATION, "environment": settings.MPESA["ENV"]},
    )


@api_view(["POST"])
def stk_push(request):
    phone_raw = (request.data.get("phone") or "").strip()
    reference = (request.data.get("reference") or "DEMO").strip()
    description = (request.data.get("description") or "Payment").strip()

    try:
        amount = int(request.data.get("amount"))
    except (TypeError, ValueError):
        return Response({"error": "Amount must be a whole number."}, status=http.HTTP_400_BAD_REQUEST)

    if amount < 1:
        return Response({"error": "Amount must be at least KES 1."}, status=http.HTTP_400_BAD_REQUEST)

    try:
        phone = normalize_phone(phone_raw)
        payload = initiate_stk_push(phone, amount, reference, description)
    except DarajaError as exc:
        logger.error("stk_push %s: %s", exc.code or "ERROR", exc.message)
        return Response({"error": exc.message, "code": exc.code}, status=exc.status)

    txn = Transaction.objects.create(
        checkout_request_id=payload["CheckoutRequestID"],
        merchant_request_id=payload.get("MerchantRequestID", ""),
        phone=phone,
        amount=amount,
        account_reference=reference[:12],
        description=description[:32],
        # "pending" is the only honest status here — a prompt was shown, nothing was paid.
        status=Transaction.Status.PENDING,
    )

    return Response(
        {
            "checkoutRequestId": txn.checkout_request_id,
            "customerMessage": payload.get("CustomerMessage", ""),
            "transaction": txn.as_dict(),
        }
    )


@csrf_exempt
@api_view(["POST"])
def mpesa_callback(request):
    """
    Where Safaricom reports the real outcome.

    Two rules: always answer 200 (anything else triggers retries, and a retry storm
    against a handler failing for its own reasons makes an outage worse), and be
    idempotent (retries mean the same payment can arrive twice).
    """
    ack = {"ResultCode": 0, "ResultDesc": "Accepted"}

    callback = (request.data or {}).get("Body", {}).get("stkCallback") or {}
    checkout_id = callback.get("CheckoutRequestID")

    if not checkout_id:
        logger.error("callback: unrecognised payload shape %s", str(request.data)[:400])
        return Response(ack)

    result_code = int(callback.get("ResultCode", -1))
    result_desc = callback.get("ResultDesc", "")

    items = {i.get("Name"): i.get("Value") for i in (callback.get("CallbackMetadata") or {}).get("Item", [])}
    receipt = items.get("MpesaReceiptNumber")
    paid_amount = items.get("Amount")

    logger.info("callback %s -> %s (%s)", checkout_id, result_code, result_desc)

    with db_transaction.atomic():
        txn = Transaction.objects.select_for_update().filter(checkout_request_id=checkout_id).first()

        if txn is None:
            # Normal after a rebuilt database; in production it means a payment
            # exists that we have no record of, which someone must chase.
            logger.warning("callback: no local record for %s — payment may be unreconciled", checkout_id)
            return Response(ack)

        if txn.is_settled:
            logger.info("callback: %s already %s, ignoring duplicate", checkout_id, txn.status)
            return Response(ack)

        txn.status = status_for(result_code)
        txn.result_code = result_code
        txn.result_desc = result_desc[:255]
        txn.mpesa_receipt = receipt or None
        txn.transaction_date = str(items.get("TransactionDate") or "")
        txn.raw_callback = request.data
        txn.save()

    if paid_amount is not None and int(round(float(paid_amount))) != txn.amount:
        logger.error(
            "callback: AMOUNT MISMATCH on %s — requested %s, received %s", checkout_id, txn.amount, paid_amount
        )

    return Response(ack)


@api_view(["GET"])
def stk_status(request):
    checkout_id = request.query_params.get("id")
    if not checkout_id:
        return Response({"error": "Missing ?id= parameter."}, status=http.HTTP_400_BAD_REQUEST)

    txn = Transaction.objects.filter(checkout_request_id=checkout_id).first()
    if txn is None:
        return Response({"error": "Unknown transaction."}, status=http.HTTP_404_NOT_FOUND)

    overdue = txn.status == Transaction.Status.PENDING and timezone.now() - txn.created_at > timedelta(seconds=20)
    if not overdue:
        return Response({"transaction": txn.as_dict(), "source": "local"})

    try:
        result = query_stk_status(checkout_id)
    except DarajaError as exc:
        logger.error("stk_status %s: %s", exc.code or "ERROR", exc.message)
        # The stored row is still the best answer we have.
        return Response({"transaction": txn.as_dict(), "source": "local", "note": exc.message})

    # While the customer still has the prompt open Daraja answers 500.001.1001
    # ("transaction is being processed"). Not an error — keep waiting.
    if result.get("errorCode") == "500.001.1001":
        return Response({"transaction": txn.as_dict(), "source": "daraja", "note": "still awaiting customer"})

    if result.get("ResultCode") is None:
        return Response(
            {"transaction": txn.as_dict(), "source": "daraja", "note": result.get("errorMessage", "No result yet.")}
        )

    result_code = int(result["ResultCode"])
    txn.status = status_for(result_code)
    txn.result_code = result_code
    txn.result_desc = (result.get("ResultDesc") or "")[:255]
    txn.save(update_fields=["status", "result_code", "result_desc", "updated_at"])

    return Response({"transaction": txn.as_dict(), "source": "daraja"})


@api_view(["GET"])
def transactions(request):
    # Sweep abandoned prompts before reporting, so the totals below are honest.
    expire_stale_pending()

    qs = Transaction.objects.all()
    failed_states = [Transaction.Status.FAILED, Transaction.Status.CANCELLED, Transaction.Status.TIMEOUT]

    agg = qs.aggregate(
        total=Count("id"),
        collected=Sum("amount", filter=Q(status=Transaction.Status.SUCCESS)),
        successful=Count("id", filter=Q(status=Transaction.Status.SUCCESS)),
        pending=Count("id", filter=Q(status=Transaction.Status.PENDING)),
        failed=Count("id", filter=Q(status__in=failed_states)),
        # A settled payment with no receipt cannot be matched against the M-Pesa
        # statement. This is the queue a finance team works by hand every morning.
        unreconciled=Count("id", filter=Q(status=Transaction.Status.SUCCESS, mpesa_receipt__isnull=True)),
    )
    agg["collected"] = agg["collected"] or 0

    return Response({"summary": agg, "transactions": [t.as_dict() for t in qs]})


@api_view(["POST"])
def simulate_callback(request):
    """
    Demo aid: fabricates the callback Safaricom would have sent and runs it through
    the real handler, so the end-to-end flow can be shown with no public tunnel.

    It proves the callback handling works. It does not prove Safaricom can reach
    you — that still has to be tested against the sandbox before going live.
    """
    if not settings.ALLOW_SIMULATION:
        return Response(
            {"error": "Simulation is disabled. Set ALLOW_SIMULATION=true in .env to enable it."},
            status=http.HTTP_403_FORBIDDEN,
        )

    checkout_id = request.data.get("id")
    outcome = request.data.get("outcome", "success")

    txn = Transaction.objects.filter(checkout_request_id=checkout_id).first()
    if txn is None:
        return Response({"error": "Unknown transaction."}, status=http.HTTP_404_NOT_FOUND)

    outcomes = {
        "success": (0, "The service request is processed successfully."),
        "cancelled": (1032, "Request cancelled by user"),
        "insufficient": (1, "The balance is insufficient for the transaction"),
    }
    code, desc = outcomes.get(outcome, outcomes["success"])

    stk = {
        "MerchantRequestID": txn.merchant_request_id,
        "CheckoutRequestID": txn.checkout_request_id,
        "ResultCode": code,
        "ResultDesc": desc,
    }

    if code == 0:
        receipt = "S" + "".join(random.choices(string.ascii_uppercase + string.digits, k=9))
        stk["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": txn.amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": int(nairobi_timestamp())},
                {"Name": "PhoneNumber", "Value": int(txn.phone)},
            ]
        }

    payload = {"Body": {"stkCallback": stk}}

    # Go through the real handler rather than writing to the model directly, so this
    # exercises the same parsing and idempotency code the live callback uses.
    inner = APIRequestFactory().post("/api/mpesa/callback/", payload, format="json")
    mpesa_callback(inner)

    txn.refresh_from_db()
    return Response({"delivered": True, "simulated": payload, "transaction": txn.as_dict()})
