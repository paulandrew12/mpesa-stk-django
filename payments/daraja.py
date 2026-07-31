"""
Safaricom Daraja client.

Three things about this API cost everyone a day the first time:

1. ``Timestamp`` and ``Password`` must be derived from the same instant, in East
   Africa Time. When they disagree Daraja answers "Invalid Access Token", which
   sends you off checking credentials that were never the problem.
2. A 200 on the push means the prompt was delivered, not that anyone paid. The
   real outcome only arrives on the callback.
3. Callbacks get lost and callbacks get duplicated. You need both a retry-safe
   handler and a way to ask Daraja directly.
"""

import base64
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings

logger = logging.getLogger("payments")

TIMEOUT = 30
EAT = timezone(timedelta(hours=3))


class DarajaError(Exception):
    def __init__(self, message: str, status: int = 502, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def base_url() -> str:
    if settings.MPESA["ENV"] == "production":
        return "https://api.safaricom.co.ke"
    return "https://sandbox.safaricom.co.ke"


def _require(key: str) -> str:
    value = settings.MPESA.get(key)
    if not value:
        raise DarajaError(
            f"Missing MPESA_{key}. Copy .env.example to .env and fill in your Daraja credentials.",
            status=500,
            code="CONFIG_MISSING",
        )
    return value


def nairobi_timestamp() -> str:
    """EAT is UTC+3 year round, so a fixed offset is correct and avoids depending
    on the server's local timezone."""
    return datetime.now(EAT).strftime("%Y%m%d%H%M%S")


def normalize_phone(raw: str) -> str:
    """Accepts 0712…, 254712…, +254712…, 712… and returns the 2547XXXXXXXX form
    Daraja requires."""
    digits = re.sub(r"\D", "", raw or "")

    if digits.startswith("254"):
        local = digits[3:]
    elif digits.startswith("0"):
        local = digits[1:]
    else:
        local = digits

    if not re.fullmatch(r"[17]\d{8}", local):
        raise DarajaError(
            f'"{raw}" is not a valid Kenyan mobile number. Expected a Safaricom or Airtel line, e.g. 0712345678.',
            status=400,
            code="INVALID_PHONE",
        )

    return f"254{local}"


# Tokens last about an hour; refetching per request is a needless round trip.
_token_cache: dict[str, float | str] = {"value": "", "expires_at": 0.0}


def get_access_token() -> str:
    if _token_cache["value"] and time.time() < float(_token_cache["expires_at"]):
        return str(_token_cache["value"])

    key = _require("CONSUMER_KEY")
    secret = _require("CONSUMER_SECRET")
    credentials = base64.b64encode(f"{key}:{secret}".encode()).decode()

    try:
        response = requests.get(
            f"{base_url()}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {credentials}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DarajaError(f"Could not reach Daraja: {exc}", status=502, code="NETWORK") from exc

    if response.status_code != 200:
        raise DarajaError(
            f"Could not authenticate with Daraja (HTTP {response.status_code}). Check your consumer key "
            f"and secret, and that they belong to the {settings.MPESA['ENV']} environment. "
            f"Response: {response.text[:200]}",
            status=response.status_code,
            code="AUTH_FAILED",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DarajaError(f"Daraja returned a non-JSON auth response: {response.text[:200]}", code="BAD_RESPONSE") from exc

    token = payload.get("access_token")
    if not token:
        raise DarajaError("Daraja auth response contained no access_token.", code="BAD_RESPONSE")

    # Expire our copy a minute early so a token never dies mid-request.
    ttl = int(payload.get("expires_in", 3599))
    _token_cache["value"] = token
    _token_cache["expires_at"] = time.time() + max(ttl - 60, 30)

    return token


def _password(timestamp: str) -> tuple[str, str]:
    shortcode = _require("SHORTCODE")
    passkey = _require("PASSKEY")
    return shortcode, base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()


def initiate_stk_push(phone: str, amount: int, account_reference: str, description: str) -> dict:
    callback_base = _require("CALLBACK_BASE_URL").rstrip("/")

    if callback_base.startswith(("http://localhost", "http://127.")):
        raise DarajaError(
            "MPESA_CALLBACK_BASE_URL points at localhost. Safaricom's servers cannot reach your machine — "
            "run `ngrok http 8000` and use the public HTTPS URL it gives you.",
            status=500,
            code="CONFIG_INVALID",
        )

    timestamp = nairobi_timestamp()
    shortcode, password = _password(timestamp)

    body = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        # Daraja rejects decimals; round before it does it for you.
        "Amount": int(round(amount)),
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": f"{callback_base}/api/mpesa/callback/",
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    }

    try:
        response = requests.post(
            f"{base_url()}/mpesa/stkpush/v1/processrequest",
            json=body,
            headers={"Authorization": f"Bearer {get_access_token()}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DarajaError(f"Could not reach Daraja: {exc}", status=502, code="NETWORK") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise DarajaError(f"Daraja returned a non-JSON response: {response.text[:200]}", code="BAD_RESPONSE") from exc

    if response.status_code != 200 or payload.get("errorCode"):
        raise DarajaError(
            payload.get("errorMessage") or f"STK push failed with HTTP {response.status_code}",
            status=response.status_code if 400 <= response.status_code < 600 else 502,
            code=payload.get("errorCode"),
        )

    if str(payload.get("ResponseCode")) != "0":
        raise DarajaError(
            payload.get("ResponseDescription") or "Daraja declined the STK push.",
            status=400,
            code=str(payload.get("ResponseCode")),
        )

    return payload


def query_stk_status(checkout_request_id: str) -> dict:
    """Ask Daraja for an outcome directly — the recovery path for lost callbacks."""
    timestamp = nairobi_timestamp()
    shortcode, password = _password(timestamp)

    try:
        response = requests.post(
            f"{base_url()}/mpesa/stkpushquery/v1/query",
            json={
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "CheckoutRequestID": checkout_request_id,
            },
            headers={"Authorization": f"Bearer {get_access_token()}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DarajaError(f"Could not reach Daraja: {exc}", status=502, code="NETWORK") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise DarajaError(f"Daraja returned a non-JSON query response: {response.text[:200]}", code="BAD_RESPONSE") from exc
