import hashlib
import hmac
import json
from types import SimpleNamespace
import time
from unittest.mock import Mock

from bs4 import BeautifulSoup
import pytest
import stripe

from app import create_app
import payments


@pytest.fixture
def setup(tmp_path):
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test",
        "DATABASE": str(tmp_path / "payments.sqlite3"),
        "SIBC_STRIPE_SECRET_KEY": "rk_test_placeholder",
        "SIBC_STRIPE_WEBHOOK_SECRET": "whsec_local_test_only",
        "SIBC_STRIPE_ACCOUNT_ID": "acct_sibc_test",
        "SIBC_STRIPE_TEST_CHECKOUT_ENABLED": "true",
    })
    gateway = SimpleNamespace(v1=SimpleNamespace(
        accounts=Mock(), checkout=SimpleNamespace(sessions=Mock()),
        subscriptions=Mock(), invoices=Mock(), charges=Mock(),
    ))
    gateway.v1.accounts.retrieve_current.return_value = {"id": "acct_sibc_test"}
    gateway.v1.checkout.sessions.create.return_value = {"id": "cs_test_1", "livemode": False, "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
    app.extensions["stripe_client_factory"] = lambda: gateway
    return app, gateway


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1.001", "1e2", "10000.01", "", " 25", True, 25, None])
def test_invalid_amounts(value):
    with pytest.raises(payments.PaymentInputError):
        payments.parse_amount(value)


def test_exact_decimal_amount():
    assert payments.parse_amount("10.29") == 1029
    assert payments.parse_amount("1") == 100
    assert payments.parse_amount("10000") == 1_000_000


def begin(app):
    client = app.test_client()
    soup = BeautifulSoup(client.get("/en/donations/").text, "html.parser")
    token = soup.select_one("[name=attempt_id]")["value"]
    csrf = soup.select_one("[name=csrf]")["value"]
    return client, token, {"X-CSRF-Token": csrf}


@pytest.mark.parametrize("frequency,mode", [("one_time", "payment"), ("monthly", "subscription")])
def test_checkout_validates_and_uses_separate_test_configuration(setup, frequency, mode):
    app, gateway = setup
    client, token, headers = begin(app)
    response = client.post("/api/create-donation-checkout", json={"amount": "25.19", "frequency": frequency, "attempt_id": token, "monthly_consent": True}, headers=headers)
    assert response.status_code == 200
    params = gateway.v1.checkout.sessions.create.call_args.args[0]
    assert params["mode"] == mode
    assert params["line_items"][0]["price_data"]["unit_amount"] == 2519
    assert "payment_method_types" not in params
    assert "{CHECKOUT_SESSION_ID}" in params["success_url"]
    assert params["metadata"]["application"] == "sibc"
    assert gateway.v1.checkout.sessions.create.call_args.kwargs["options"]["idempotency_key"] == "sibc-" + token
    response = client.post("/api/create-donation-checkout", json={"amount": "30", "frequency": frequency, "attempt_id": token, "monthly_consent": True}, headers=headers)
    assert response.status_code == 400


def test_monthly_consent_and_safe_provider_failure(setup):
    app, gateway = setup
    client, token, headers = begin(app)
    body = {"amount": "25", "frequency": "monthly", "attempt_id": token}
    assert client.post("/api/create-donation-checkout", json=body, headers=headers).status_code == 400
    gateway.v1.checkout.sessions.create.side_effect = stripe.APIConnectionError("secret upstream error")
    body["monthly_consent"] = True
    result = client.post("/api/create-donation-checkout", json=body, headers=headers)
    assert result.status_code == 502
    assert "secret upstream error" not in result.text


def seed(app, frequency="one_time"):
    with payments.database(app.config["DATABASE"]) as db:
        db.execute("INSERT INTO attempts VALUES ('attempt-1', 2500, ?, 'cs_test_1', 0)", (frequency,))


def checkout_object(paid=True, **extra):
    return {
        "id": "cs_test_1", "livemode": False, "metadata": {"application": "sibc", "attempt_id": "attempt-1"},
        "payment_status": "paid" if paid else "unpaid", "amount_total": 2500, "currency": "usd",
        "mode": "payment", "payment_intent": "pi_test_1", **extra,
    }


def event(kind="checkout.session.completed", event_id="evt_test_1", object_id="cs_test_1"):
    return {"id": event_id, "type": kind, "livemode": False, "data": {"object": {"id": object_id}}}


def send(client, app, payload, valid=True):
    body = json.dumps(payload).encode()
    timestamp = int(time.time())
    signature = hmac.new(app.config["SIBC_STRIPE_WEBHOOK_SECRET"].encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return client.post("/api/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={timestamp},v1={signature if valid else 'bad'}", "Content-Type": "application/json"})


def test_signature_duplicates_and_distinct_events_are_idempotent(setup):
    app, gateway = setup
    seed(app)
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object()
    client = app.test_client()
    assert send(client, app, event(), valid=False).status_code == 400
    assert send(client, app, event()).status_code == 200
    assert send(client, app, event()).json["status"] == "duplicate"
    assert send(client, app, event("checkout.session.async_payment_succeeded", "evt_other")).status_code == 200
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM donations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert db.execute("SELECT status FROM donations").fetchone()[0] == "paid"


def test_delayed_failure_then_success_and_out_of_order_event(setup):
    app, gateway = setup
    seed(app)
    client = app.test_client()
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object(paid=False)
    send(client, app, event())
    send(client, app, event("checkout.session.async_payment_failed", "evt_failure"))
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT status FROM donations").fetchone()[0] == "failed"
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object()
    send(client, app, event("checkout.session.async_payment_succeeded", "evt_paid"))
    send(client, app, event("checkout.session.async_payment_failed", "evt_old_failure"))
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT status FROM donations").fetchone()[0] == "paid"


def test_reconciliation_failure_retries_without_marking_processed(setup):
    app, gateway = setup
    seed(app)
    gateway.v1.checkout.sessions.retrieve.side_effect = stripe.APIConnectionError("temporary")
    response = send(app.test_client(), app, event())
    assert response.status_code == 503
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_subscription_invoice_renewal_failure_and_cancellation(setup):
    app, gateway = setup
    seed(app, "monthly")
    subscription = {"id": "sub_test_1", "livemode": False, "status": "active", "metadata": {"application": "sibc", "attempt_id": "attempt-1"}}
    gateway.v1.subscriptions.retrieve.return_value = subscription
    gateway.v1.invoices.retrieve.return_value = {"id": "in_test_1", "livemode": False, "status": "paid", "amount_paid": 2500, "currency": "usd", "parent": {"subscription_details": {"subscription": "sub_test_1"}}}
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object(mode="subscription", subscription="sub_test_1", invoice="in_test_1")
    client = app.test_client()
    assert send(client, app, event()).status_code == 200
    assert send(client, app, event("invoice.paid", "evt_invoice", "in_test_1")).status_code == 200
    gateway.v1.invoices.retrieve.return_value = {"id": "in_test_2", "livemode": False, "status": "open", "attempted": True, "amount_due": 2500, "currency": "usd", "subscription": "sub_test_1"}
    assert send(client, app, event("invoice.payment_failed", "evt_renewal", "in_test_2")).status_code == 200
    gateway.v1.subscriptions.retrieve.return_value = {**subscription, "status": "canceled"}
    assert send(client, app, event("customer.subscription.deleted", "evt_canceled", "sub_test_1")).status_code == 200
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT COUNT(*) FROM donations").fetchone()[0] == 2
        assert db.execute("SELECT status FROM donations WHERE id='in_test_2'").fetchone()[0] == "failed"
        assert db.execute("SELECT status FROM subscriptions").fetchone()[0] == "canceled"


def test_amount_mismatch_and_live_events_rejected(setup):
    app, gateway = setup
    seed(app)
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object(amount_total=100)
    assert send(app.test_client(), app, event()).status_code == 400
    assert send(app.test_client(), app, {**event(), "livemode": True}).status_code == 400
    assert send(app.test_client(), app, {**event(), "account": "acct_other"}).status_code == 400


def test_refund_is_not_overwritten_by_later_checkout_event(setup):
    app, gateway = setup
    seed(app)
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object()
    gateway.v1.charges.retrieve.return_value = {"id": "ch_test_1", "livemode": False, "refunded": True, "amount_refunded": 2500, "payment_intent": "pi_test_1"}
    client = app.test_client()
    send(client, app, event())
    send(client, app, event("charge.refunded", "evt_refund", "ch_test_1"))
    send(client, app, event("checkout.session.completed", "evt_retry_checkout"))
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT status FROM donations").fetchone()[0] == "refunded"


def test_real_sdk_objects_and_webhook_backed_return_verification(setup):
    app, gateway = setup
    seed(app)
    gateway.v1.checkout.sessions.retrieve.return_value = stripe.StripeObject.construct_from(checkout_object(), None)
    client = app.test_client()
    with client.session_transaction() as state:
        state["last_attempt"] = "attempt-1"
    assert "Payment is not confirmed" in client.get("/en/donations/return/?session_id=cs_test_1").text
    assert send(client, app, event()).status_code == 200
    assert "Test payment verified" in client.get("/en/donations/return/?session_id=cs_test_1").text
    gateway.v1.checkout.sessions.retrieve.return_value = stripe.StripeObject.construct_from(checkout_object(amount_total=100), None)
    assert "Payment is not confirmed" in client.get("/en/donations/return/?session_id=cs_test_1").text


def test_expired_session_is_not_paid_and_insecure_checkout_rejected(setup):
    app, gateway = setup
    seed(app)
    gateway.v1.checkout.sessions.retrieve.return_value = checkout_object(paid=False)
    assert send(app.test_client(), app, event("checkout.session.expired")).status_code == 200
    with payments.database(app.config["DATABASE"]) as db:
        assert db.execute("SELECT status FROM donations").fetchone()[0] == "expired"
    gateway.v1.checkout.sessions.create.return_value = {"id": "cs_test_2", "livemode": False, "url": "http://checkout.stripe.com/not-secure"}
    with pytest.raises(payments.PaymentInputError):
        payments.create_checkout(app.config, app.config["DATABASE"], "attempt-new", 2500, "one_time", gateway)


def test_key_owner_must_match_sibc_not_a_connected_account(setup):
    app, gateway = setup
    gateway.v1.accounts.retrieve_current.return_value = {"id": "acct_another_organization"}
    with pytest.raises(payments.PaymentInputError):
        payments.create_checkout(app.config, app.config["DATABASE"], "attempt-new", 2500, "one_time", gateway)
    gateway.v1.checkout.sessions.create.assert_not_called()
