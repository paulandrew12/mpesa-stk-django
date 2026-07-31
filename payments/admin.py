from django.contrib import admin

from .models import Transaction


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "phone", "amount", "account_reference", "status", "mpesa_receipt")
    list_filter = ("status", "created_at")
    search_fields = ("checkout_request_id", "phone", "mpesa_receipt", "account_reference")
    readonly_fields = tuple(f.name for f in Transaction._meta.fields)

    def has_add_permission(self, request):
        # Transactions come from Daraja, never from a person typing them in.
        return False
