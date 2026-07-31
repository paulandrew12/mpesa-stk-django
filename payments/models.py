from django.db import models


class Transaction(models.Model):
    """
    One STK Push and its outcome.

    The important line in this model is the unique constraint on ``mpesa_receipt``.
    Safaricom retries callbacks, so the same payment can arrive more than once; the
    constraint makes a double-credit impossible at the database level rather than
    relying on application code to remember to check. NULLs are exempt from the
    constraint on both SQLite and Postgres, so pending rows are unaffected.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        TIMEOUT = "timeout", "Timed out"

    checkout_request_id = models.CharField(max_length=64, unique=True, db_index=True)
    merchant_request_id = models.CharField(max_length=64)

    phone = models.CharField(max_length=15)
    amount = models.PositiveIntegerField()
    account_reference = models.CharField(max_length=12)
    description = models.CharField(max_length=32)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)

    mpesa_receipt = models.CharField(max_length=32, null=True, blank=True, unique=True)
    transaction_date = models.CharField(max_length=20, blank=True)

    # Kept verbatim. When a client disputes a payment months later, the raw payload
    # is the only thing that settles it.
    raw_callback = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.checkout_request_id} · {self.phone} · KES {self.amount} · {self.status}"

    @property
    def is_settled(self) -> bool:
        return self.status != self.Status.PENDING

    def as_dict(self) -> dict:
        return {
            "id": self.checkout_request_id,
            "merchantRequestId": self.merchant_request_id,
            "phone": self.phone,
            "amount": self.amount,
            "accountReference": self.account_reference,
            "description": self.description,
            "status": self.status,
            "resultCode": self.result_code,
            "resultDesc": self.result_desc,
            "mpesaReceipt": self.mpesa_receipt,
            "transactionDate": self.transaction_date,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }
