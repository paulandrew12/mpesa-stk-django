from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/stk/push/", views.stk_push, name="stk-push"),
    path("api/stk/status/", views.stk_status, name="stk-status"),
    path("api/mpesa/callback/", views.mpesa_callback, name="mpesa-callback"),
    path("api/transactions/", views.transactions, name="transactions"),
    path("api/dev/simulate-callback/", views.simulate_callback, name="simulate-callback"),
]
