"""Test-only SIBC Checkout and verified, idempotent payment reconciliation."""

from contextlib import contextmanager
from decimal import Decimal
import re
import sqlite3
import time
from urllib.parse import urlparse

import stripe


class PaymentInputError(ValueError):
    pass


def stripe_data(value):
    return value.to_dict() if isinstance(value, stripe.StripeObject) else value


def parse_amount(value, minimum=100, maximum=1_000_000):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", value):
        raise PaymentInputError("Enter a USD amount using up to two decimal places.")
    cents = int(Decimal(value) * 100)
    if not minimum <= cents <= maximum:
        raise PaymentInputError(f"Choose an amount from ${minimum / 100:,.2f} to ${maximum / 100:,.2f} USD.")
    return cents


@contextmanager
def database(path):
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path):
    with database(path) as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, amount INTEGER NOT NULL, frequency TEXT NOT NULL,
                session_id TEXT UNIQUE, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, processed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS donations (
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, amount INTEGER NOT NULL,
                currency TEXT NOT NULL, status TEXT NOT NULL, payment_intent TEXT
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS limits (
                actor TEXT PRIMARY KEY, started_at INTEGER NOT NULL, count INTEGER NOT NULL
            );
        """)


def rate_allowed(path, actor):
    now = int(time.time())
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM limits WHERE started_at < ?", (now - 900,))
        db.execute("INSERT INTO limits VALUES (?, ?, 1) ON CONFLICT(actor) DO UPDATE SET count=count+1", (actor, now))
        return db.execute("SELECT count FROM limits WHERE actor=?", (actor,)).fetchone()["count"] <= 20


def enabled(config):
    key = config.get("SIBC_STRIPE_SECRET_KEY", "")
    return (
        config.get("SIBC_STRIPE_TEST_CHECKOUT_ENABLED") == "true"
        and key.startswith(("sk_test_", "rk_test_"))
        and config.get("SIBC_STRIPE_WEBHOOK_SECRET", "").startswith("whsec_")
        and config.get("SIBC_STRIPE_ACCOUNT_ID", "").startswith("acct_")
    )


def client_for(config):
    return stripe.StripeClient(config["SIBC_STRIPE_SECRET_KEY"], max_network_retries=2)


def create_checkout(config, path, attempt_id, cents, frequency, client):
    if not enabled(config):
        raise PaymentInputError("Stripe checkout is unavailable. You can use the existing PayPal option or contact SIBC.")
    if frequency not in ("one_time", "monthly"):
        raise PaymentInputError("Choose one-time or monthly giving.")
    origin = config["SIBC_SITE_URL"].rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise PaymentInputError("Checkout is unavailable until the site address is configured.")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise PaymentInputError("Checkout requires a secure site address.")
    account = client.v1.accounts.retrieve_current()
    if account["id"] != config["SIBC_STRIPE_ACCOUNT_ID"]:
        raise PaymentInputError("The SIBC test recipient has not been verified.")
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if existing and (existing["amount"] != cents or existing["frequency"] != frequency):
            raise PaymentInputError("This checkout attempt already has an amount. Reload the page to start a new one.")
        if not existing:
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, NULL, ?)", (attempt_id, cents, frequency, int(time.time())))
    metadata = {"application": "sibc", "attempt_id": attempt_id}
    price = {"currency": "usd", "unit_amount": cents, "product_data": {"name": "SIBC donation (test only)"}}
    params = {
        "mode": "subscription" if frequency == "monthly" else "payment",
        "line_items": [{"quantity": 1, "price_data": price}],
        "success_url": origin + "/en/donations/return/?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": origin + "/en/donations/?checkout=canceled",
        "client_reference_id": attempt_id,
        "metadata": metadata,
    }
    if frequency == "monthly":
        price["recurring"] = {"interval": "month"}
        params["subscription_data"] = {"metadata": metadata}
    else:
        params["payment_intent_data"] = {"metadata": metadata}
    result = stripe_data(client.v1.checkout.sessions.create(params, options={"idempotency_key": "sibc-" + attempt_id}))
    destination = urlparse(result.get("url") or "")
    if result.get("livemode") is not False or destination.scheme != "https" or destination.hostname != "checkout.stripe.com":
        raise PaymentInputError("Checkout could not be verified. No live checkout is allowed.")
    with database(path) as db:
        db.execute("UPDATE attempts SET session_id=? WHERE id=?", (result["id"], attempt_id))
    return result["url"]


def object_id(value):
    return value.get("id") if isinstance(value, dict) else value


def owned_attempt(db, obj):
    metadata = obj.get("metadata") or {}
    if metadata.get("application") != "sibc" or obj.get("livemode") is not False:
        return None
    return db.execute("SELECT * FROM attempts WHERE id=?", (metadata.get("attempt_id"),)).fetchone()


def reconcile_subscription(db, client, subscription_id):
    subscription = stripe_data(client.v1.subscriptions.retrieve(subscription_id))
    attempt = owned_attempt(db, subscription)
    if not attempt or attempt["frequency"] != "monthly":
        return None
    db.execute(
        "INSERT INTO subscriptions VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
        (subscription["id"], attempt["id"], subscription["status"]),
    )
    return attempt


def reconcile_invoice(db, client, invoice_id):
    invoice = stripe_data(client.v1.invoices.retrieve(invoice_id))
    if invoice.get("livemode") is not False:
        return
    subscription_id = object_id(invoice.get("subscription")) or object_id(
        (invoice.get("parent") or {}).get("subscription_details", {}).get("subscription")
    )
    if not subscription_id:
        return
    attempt = reconcile_subscription(db, client, subscription_id)
    if not attempt:
        return
    paid = invoice.get("status") == "paid"
    amount = invoice.get("amount_paid") if paid else invoice.get("amount_due")
    if amount != attempt["amount"] or invoice.get("currency") != "usd":
        raise PaymentInputError("Invoice amount or currency does not match the donation.")
    status = "paid" if paid else "failed" if invoice.get("attempted") else "pending"
    db.execute(
        """INSERT INTO donations VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status""",
        (invoice["id"], attempt["id"], amount, "usd", status, object_id(invoice.get("payment_intent"))),
    )


def reconcile_session(db, client, session_id, event_type="checkout.session.completed"):
    checkout = stripe_data(client.v1.checkout.sessions.retrieve(session_id))
    attempt = owned_attempt(db, checkout)
    if not attempt:
        return
    if attempt["session_id"] and attempt["session_id"] != checkout["id"]:
        raise PaymentInputError("Checkout does not match this donation attempt.")
    expected_mode = "subscription" if attempt["frequency"] == "monthly" else "payment"
    if checkout.get("amount_total") != attempt["amount"] or checkout.get("currency") != "usd" or checkout.get("mode") != expected_mode:
        raise PaymentInputError("Checkout amount, currency or mode does not match.")
    if checkout["mode"] == "subscription":
        subscription_id = object_id(checkout.get("subscription"))
        if subscription_id:
            reconcile_subscription(db, client, subscription_id)
        invoice_id = object_id(checkout.get("invoice"))
        if invoice_id:
            reconcile_invoice(db, client, invoice_id)
        return
    payment_id = object_id(checkout.get("payment_intent"))
    if not payment_id:
        return
    status = "paid" if checkout.get("payment_status") == "paid" else (
        "failed" if event_type == "checkout.session.async_payment_failed" else
        "expired" if event_type == "checkout.session.expired" else "pending"
    )
    db.execute(
        """INSERT INTO donations VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status
        WHERE donations.status NOT IN ('refunded', 'partially_refunded', 'disputed')""",
        (payment_id, attempt["id"], attempt["amount"], "usd", status, payment_id),
    )


def process_event(path, event, client):
    if event.get("livemode") is not False:
        raise PaymentInputError("Live events are not accepted.")
    event_type = event["type"]
    obj = event["data"]["object"]
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone():
            return "duplicate"
        if event_type in (
            "checkout.session.completed", "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed", "checkout.session.expired",
        ):
            reconcile_session(db, client, obj["id"], event_type)
        elif event_type in ("invoice.paid", "invoice.payment_failed"):
            reconcile_invoice(db, client, obj["id"])
        elif event_type in (
            "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
        ):
            reconcile_subscription(db, client, obj["id"])
        elif event_type == "charge.refunded":
            charge = stripe_data(client.v1.charges.retrieve(obj["id"]))
            if charge.get("livemode") is False and charge.get("amount_refunded", 0) > 0:
                status = "refunded" if charge.get("refunded") else "partially_refunded"
                db.execute("UPDATE donations SET status=? WHERE payment_intent=?", (status, object_id(charge.get("payment_intent"))))
        db.execute("INSERT INTO events VALUES (?, ?, ?)", (event["id"], event_type, int(time.time())))
    return "processed"
