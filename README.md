# M-Pesa STK Push — Collections & Reconciliation (Django)

### ▶ Live demo: **https://mpesa-stk-demo.onrender.com**

Request a payment, then use **Paid** or **Cancelled** to deliver the callback Safaricom would
have sent, and watch it settle and reconcile. No real money moves — see [demo mode](#demoing-without-a-tunnel).
*(Free instance: the first request after a quiet period takes ~50s to wake.)*

---

A working Lipa Na M-Pesa Online integration: send a payment prompt to a customer's phone,
handle Safaricom's callback, recover from callbacks that never arrive, and report what is
still unreconciled.

Django 5.2 · Django REST Framework · Safaricom Daraja API · 16 tests

> A Next.js/TypeScript implementation of the same service lives in `../mpesa-stk-demo`.
> This one is the reference version: it has a real database, real migrations, and a test
> suite.

---

## The problem this solves

A business takes M-Pesa payments. Someone in finance downloads the M-Pesa statement, opens
the sales system next to it, and matches transactions by hand — usually two or three hours
a day, usually at the end of the day when they are tired.

The matching is the expensive part. A customer pays KES 1,500 but types the wrong account
number; a payment arrives twice; a payment shows in M-Pesa but never in the system because
the confirmation webhook was dropped during a deploy. Each becomes a support ticket, and
each ticket costs more than the transaction earned.

This service removes the manual step: every payment is requested against a known reference,
matched to its M-Pesa receipt automatically, and anything that fails to match is surfaced as
a number someone can act on rather than a discrepancy someone has to find.

---

## What it does

- **STK Push** — sends the PIN prompt to the customer's phone
- **Callback handling** — receives Safaricom's result, idempotently, with the raw payload
  retained for audit
- **Status recovery** — if a callback never arrives, queries Daraja directly rather than
  leaving a payment stuck as pending forever
- **Timeout sweeping** — marks abandoned prompts so daily totals stay honest
- **Reconciliation reporting** — counts settled payments with no receipt number, the queue a
  finance team would otherwise work by hand
- **Django admin** — read-only transaction browser with search and filters, for support staff

---

## Three things this API does that catch people out

**1. A 200 on the push does not mean the customer paid.**
It means the prompt was delivered. The customer may ignore it, cancel it, or have no money.
The real outcome only arrives on the callback, which is why every transaction starts as
`pending` and nothing is credited until the callback confirms it.

**2. Callbacks get lost, and callbacks get duplicated.**
Tunnels expire, deploys restart mid-flight, networks drop — and Safaricom retries, so the
same payment can arrive twice. `mpesa_callback` in [`payments/views.py`](payments/views.py)
ignores a callback for an already-settled transaction, and `stk_status` covers the opposite
case by asking Daraja directly when a callback is overdue.

Belt and braces: `mpesa_receipt` carries a **unique constraint**, so a double-credit is
impossible at the database level rather than depending on application code remembering to
check. There's a test for exactly that.

**3. The password and timestamp must come from the same instant, in EAT.**
If they disagree, Daraja returns `Invalid Access Token` — which sends you off debugging
credentials that were never the problem. See `nairobi_timestamp()` in
[`payments/daraja.py`](payments/daraja.py).

---

## Running it

### 1. Get Daraja credentials

Register at [developer.safaricom.co.ke](https://developer.safaricom.co.ke) and create an app
under **Lipa Na M-Pesa Online**. Sandbox approval is instant. Copy the Consumer Key and Secret.

### 2. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `MPESA_CONSUMER_KEY` and `MPESA_CONSUMER_SECRET`. The sandbox shortcode and passkey
are already in the file — they're the public test values Safaricom publishes.

### 3. Expose the callback URL

Safaricom's servers cannot reach `localhost`:

```bash
ngrok http 8000
```

Put the HTTPS URL into `MPESA_CALLBACK_BASE_URL`. The app refuses to start a push against a
localhost callback URL rather than letting you debug a silent failure.

### 4. Run

```bash
python manage.py migrate
python manage.py runserver
```

Open http://localhost:8000. In the sandbox, use one of Safaricom's test MSISDNs — a real
number will not receive a sandbox prompt.

### Tests

```bash
python manage.py test payments
```

16 tests covering phone normalisation, callback settlement, idempotency under retry, orphan
and malformed callbacks, the database uniqueness constraint, reconciliation totals, timeout
sweeping, and the simulation gate.

### Demoing without a tunnel

With `ALLOW_SIMULATION=true`, each pending transaction gets **Paid** / **Cancelled** buttons
that post a synthetic Safaricom callback through the real handler — useful for showing the
flow on a laptop with no public URL. It proves the callback handling works; it does not prove
Safaricom can reach you, so still test against the sandbox before going live.

Set `ALLOW_SIMULATION=false` in production.

---

## Layout

```
config/            settings, urls, wsgi
payments/
  daraja.py        Daraja client: auth, token cache, push, status query
  models.py        Transaction — unique receipt constraint lives here
  views.py         push · callback · status · transactions · simulate
  admin.py         read-only transaction browser
  tests.py         16 tests
  templates/       dashboard
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/stk/push/` | Initiate a payment; stores it as pending |
| `POST` | `/api/mpesa/callback/` | Safaricom's result — idempotent, always 200 |
| `GET` | `/api/stk/status/?id=` | Read a transaction; query Daraja if the callback is overdue |
| `GET` | `/api/transactions/` | List plus reconciliation summary |
| `POST` | `/api/dev/simulate-callback/` | Demo aid, env-gated |

---

## Before this goes near real money

- **Move to Postgres.** Change `DATABASES` in [`config/settings.py`](config/settings.py); the
  schema and constraints are already correct. SQLite is here so the demo runs in one command.
- **Validate callback origin.** This handler accepts any POST. Restrict to Safaricom's
  published IP ranges, and treat the callback as a trigger to verify rather than as trusted
  data.
- **Make credit operations transactional.** Marking a payment settled and crediting the
  customer's balance must succeed or fail together. The callback handler already opens an
  atomic block with `select_for_update` — extend it rather than adding a second transaction.
- **Add a daily reconciliation job** that pulls the M-Pesa statement and compares it against
  the ledger. The `unreconciled` counter is the live view; the daily job catches what the
  live view missed.
- **Rate-limit the push endpoint.** It costs money per call and is trivially abusable.
- **Set `DJANGO_DEBUG=false`** and a real `DJANGO_SECRET_KEY`.

---

## Deploying

Needs a stable public HTTPS URL for the callback. Railway, Render, Fly, or any VPS works.
Run `python manage.py collectstatic` and serve behind gunicorn.
