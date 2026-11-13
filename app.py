import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlparse
import uuid
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup
import stripe

import payments

ROOT = Path(__file__).parent
PAGES = {page["path"]: page for page in json.loads((ROOT / "content/legacy-pages.json").read_text())}
LOCALES = json.loads((ROOT / "content/locales.json").read_text())
ASSETS = json.loads((ROOT / "content/asset-sources.json").read_text())
ASSET_PATHS = {
    urlparse(asset["source_url"]).path: asset["file"]
    for asset in ASSETS if asset["source_url"].startswith("https://sibc-tukwila.org/")
}
MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}
MASTER_ID = "4j5qf0tpnggubvq6s8mpgfhcl4"
DANA_ID = "4ca10b431eo7c8p50f7qmvo96k"
CALENDAR = f"https://calendar.google.com/calendar/embed?src={MASTER_ID}%40group.calendar.google.com&ctz=America%2FLos_Angeles"
DANA = f"https://calendar.google.com/calendar/embed?src={DANA_ID}%40group.calendar.google.com&ctz=America%2FLos_Angeles"
ICS = f"https://calendar.google.com/calendar/ical/{MASTER_ID}%40group.calendar.google.com/public/basic.ics"
MAP = "https://www.google.com/maps/search/?api=1&query=Sarana+International+Buddhist+Center+15241+51st+Ave+S+Tukwila+WA+98188"
NAV_PATHS = ("/", "/about/", "/activities/", "/events/calendar/", "/donations/", "/contact/")
CORE = {
    "/": ("Home", "home"),
    "/about/": ("A temple open to everyone.", "about"),
    "/activities/": ("Practice, learn, connect.", "activities"),
    "/about/address/": ("Find us in Tukwila.", "contact"),
    "/contact/": ("Get in touch.", "contact"),
    "/events/": ("The temple calendar.", "calendar"),
    "/events/calendar/": ("The temple calendar.", "calendar"),
    "/events/upcoming/": ("Upcoming information, honestly.", "calendar"),
    "/events/archived/": ("Our community, through the years.", "archive"),
    "/donations/": ("Support a place of peace.", "donate"),
    "/donations/campaigns/": ("Temple projects, in context.", "campaigns"),
    "/donations/dana_schedule/": ("The monthly Dana calendar.", "dana"),
    "/about/media/": ("A glimpse of SIBC.", "media"),
    "/news/": ("Stories from the temple.", "news"),
    "/review/": ("Ready for SIBC's review.", "review"),
    "/search/": ("Find existing temple content.", "search"),
    "/donations/return/": ("Your checkout status.", "return"),
}


def event_details():
    return sorted(
        [page for path, page in PAGES.items() if re.search(r"^/events/\d{4}/", path) and path.rstrip("/").split("/")[-1] not in MONTHS],
        key=lambda page: page["path"], reverse=True,
    )


def source_html(html, locale):
    soup = BeautifulSoup(html, "html5lib")
    for anchor in soup.select("a[href]"):
        href = anchor["href"]
        if href.startswith("/") and not href.startswith("//"):
            anchor["href"] = "/" + locale + href
    return Markup(soup.body.decode_contents())


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=512_000,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SIBC_SITE_URL", "").startswith("https://"),
        SIBC_STRIPE_SECRET_KEY=os.environ.get("SIBC_STRIPE_SECRET_KEY", ""),
        SIBC_STRIPE_WEBHOOK_SECRET=os.environ.get("SIBC_STRIPE_WEBHOOK_SECRET", ""),
        SIBC_STRIPE_ACCOUNT_ID=os.environ.get("SIBC_STRIPE_ACCOUNT_ID", ""),
        SIBC_STRIPE_TEST_CHECKOUT_ENABLED=os.environ.get("SIBC_STRIPE_TEST_CHECKOUT_ENABLED", "false"),
        SIBC_SITE_URL=os.environ.get("SIBC_SITE_URL", "http://127.0.0.1:8080"),
        DONATION_MIN=int(os.environ.get("SIBC_DONATION_MIN_CENTS", "100")),
        DONATION_MAX=int(os.environ.get("SIBC_DONATION_MAX_CENTS", "1000000")),
        DATABASE=str(Path(app.instance_path) / "donations.sqlite3"),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(exist_ok=True)
    payments.initialize(app.config["DATABASE"])
    app.extensions["stripe_client_factory"] = lambda: payments.client_for(app.config)

    @app.after_request
    def secure_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-src https://calendar.google.com; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        if request.path.startswith("/api/") or "/donations/" in request.path:
            response.headers["Cache-Control"] = "no-store"
        return response

    def page_response(locale, path, status=200, error=None):
        if locale not in LOCALES:
            abort(404)
        if path not in CORE and path not in PAGES and status != 404:
            abort(404)
        title, kind = CORE.get(path, (PAGES.get(path, {}).get("title", "Page not found"), "legacy"))
        if status == 404:
            title, kind = "This page isn't here.", "not-found"
        page = PAGES.get(path)
        session.setdefault("csrf", secrets.token_hex(32))
        if kind == "donate":
            session["checkout_token"] = str(uuid.uuid4())
        state = "unconfirmed"
        if kind == "return":
            session_id = request.args.get("session_id", "")
            attempt_id = session.get("last_attempt")
            with payments.database(app.config["DATABASE"]) as db:
                attempt = db.execute("SELECT * FROM attempts WHERE id=? AND session_id=?", (attempt_id, session_id)).fetchone()
                record = db.execute("SELECT status FROM donations WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1", (attempt_id,)).fetchone() if attempt else None
            if attempt and record and payments.enabled(app.config):
                try:
                    verified = payments.stripe_data(app.extensions["stripe_client_factory"]().v1.checkout.sessions.retrieve(session_id))
                    if (
                        verified.get("livemode") is False
                        and verified.get("metadata", {}).get("attempt_id") == attempt_id
                        and verified.get("metadata", {}).get("application") == "sibc"
                        and verified.get("amount_total") == attempt["amount"]
                        and verified.get("currency") == "usd"
                        and verified.get("mode") == ("subscription" if attempt["frequency"] == "monthly" else "payment")
                    ):
                        state = record["status"] if verified.get("payment_status") == "paid" else "pending"
                except stripe.StripeError:
                    app.logger.warning("Stripe return verification unavailable")
        return render_template(
            "page.html", locale=locale, language=LOCALES[locale], locales=LOCALES,
            path=path, title=title, kind=kind, page=page, error=error,
            nav=list(zip(NAV_PATHS, LOCALES[locale]["nav"])), assets=ASSETS,
            legacy_html=source_html(page["html"], locale) if page else "",
            events=event_details(), news=[p for p in PAGES.values() if re.search(r"^/news/\d{4}/[^/]+/", p["path"])],
            calendar_url=CALENDAR, dana_url=DANA, ics_url=ICS, map_url=MAP,
            stripe_enabled=payments.enabled(app.config), payment_state=state,
            min_amount=app.config["DONATION_MIN"] / 100, max_amount=app.config["DONATION_MAX"] / 100,
        ), status

    @app.get("/")
    def root():
        return redirect("/en/", 302)

    @app.get("/<locale>/")
    def locale_home(locale):
        if locale not in LOCALES:
            path = "/" + locale + "/"
            if path in CORE or path in PAGES:
                return redirect("/en" + path, 301)
            if path == "/redirect-page/":
                return redirect("/en/", 301)
            abort(404)
        return page_response(locale, "/")

    @app.get("/<locale>/<path:slug>/")
    def localized(locale, slug):
        if locale not in LOCALES:
            legacy = "/" + locale + "/" + slug + "/"
            if legacy in PAGES or legacy in CORE:
                return redirect("/en" + legacy, 301)
            abort(404)
        return page_response(locale, "/" + slug + "/")

    @app.get("/<path:slug>")
    def legacy_redirect(slug):
        path = "/" + slug.strip("/") + "/"
        if path == "/redirect-page/":
            return redirect("/en/", 301)
        if path in CORE or path in PAGES:
            return redirect("/en" + path, 301)
        abort(404)

    @app.get("/images/<path:filename>")
    def original_image(filename):
        destination = ASSET_PATHS.get("/images/" + filename)
        if not destination:
            abort(404)
        return redirect(destination, 301)

    @app.get("/robots.txt")
    def robots():
        return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}

    @app.get("/humans.txt")
    def humans():
        return "SIBC local review build. Content source: https://sibc-tukwila.org/\n", 200, {"Content-Type": "text/plain"}

    @app.get("/sitemap.xml")
    def sitemap():
        base = app.config["SIBC_SITE_URL"].rstrip("/")
        entries = "".join(f"<url><loc>{escape(base + '/en' + path)}</loc></url>" for path in sorted(set(CORE) | set(PAGES)) if path not in ("/review/", "/donations/return/"))
        return '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + "</urlset>", 200, {"Content-Type": "application/xml"}

    @app.get("/feed.xml")
    @app.get("/atom.xml")
    def feeds():
        return redirect("https://sibc-tukwila.org" + request.path, 302)

    @app.errorhandler(404)
    def not_found(_error):
        locale = request.path.strip("/").split("/")[0]
        return page_response(locale if locale in LOCALES else "en", "/missing/", 404)

    @app.post("/api/create-donation-checkout")
    def checkout():
        body = request.get_json(silent=True) if request.is_json else request.form
        if not isinstance(body, dict) and request.is_json:
            return jsonify(error="Send a valid donation request."), 400
        token = request.headers.get("X-CSRF-Token") or body.get("csrf", "")
        if not isinstance(token, str) or not session.get("csrf") or not hmac.compare_digest(token, session["csrf"]):
            return jsonify(error="Your form has expired. Reload the donation page and try again."), 403
        if not payments.enabled(app.config):
            return jsonify(error="Stripe checkout is disabled. Please use PayPal or contact SIBC."), 503
        attempt = body.get("attempt_id")
        if not isinstance(attempt, str) or attempt != session.get("checkout_token"):
            return jsonify(error="Reload the donation page to start a new checkout."), 400
        actor = hashlib.sha256((app.config["SECRET_KEY"] + (request.remote_addr or "local")).encode()).hexdigest()
        if not payments.rate_allowed(app.config["DATABASE"], actor):
            return jsonify(error="Too many checkout attempts. Please wait 15 minutes or contact SIBC."), 429
        try:
            cents = payments.parse_amount(body.get("amount"), app.config["DONATION_MIN"], app.config["DONATION_MAX"])
            frequency = body.get("frequency")
            if frequency not in ("one_time", "monthly"):
                raise payments.PaymentInputError("Choose one-time or monthly giving.")
            if frequency == "monthly" and body.get("monthly_consent") not in (True, "yes"):
                raise payments.PaymentInputError("Confirm that you understand this is a monthly donation.")
            checkout_url = payments.create_checkout(
                app.config, app.config["DATABASE"], attempt, cents, frequency,
                app.extensions["stripe_client_factory"](),
            )
        except payments.PaymentInputError as exc:
            return jsonify(error=str(exc)), 400
        except (stripe.StripeError, sqlite3.Error):
            app.logger.error("Stripe checkout unavailable; attempt=%s", attempt)
            return jsonify(error="Checkout could not start. Please try again later or use PayPal. Do not retry if you already completed a payment."), 502
        session["last_attempt"] = attempt
        if request.is_json:
            return jsonify(url=checkout_url)
        return redirect(checkout_url, 303)

    @app.post("/api/stripe/webhook")
    def webhook():
        if not payments.enabled(app.config):
            return jsonify(error="Stripe integration is disabled."), 503
        try:
            event = stripe.Webhook.construct_event(
                request.get_data(cache=False), request.headers.get("Stripe-Signature", ""),
                app.config["SIBC_STRIPE_WEBHOOK_SECRET"],
            ).to_dict()
        except (ValueError, stripe.SignatureVerificationError):
            return jsonify(error="Invalid webhook signature or payload."), 400
        if event.get("account") and event["account"] != app.config["SIBC_STRIPE_ACCOUNT_ID"]:
            return jsonify(error="Unexpected Stripe account."), 400
        try:
            result = payments.process_event(app.config["DATABASE"], event, app.extensions["stripe_client_factory"]())
        except (KeyError, TypeError, payments.PaymentInputError):
            app.logger.warning("Webhook validation failed; event=%s", event.get("id", "invalid"))
            return jsonify(error="Event validation failed."), 400
        except (stripe.StripeError, sqlite3.Error):
            app.logger.error("Webhook reconciliation failed; event=%s", event.get("id", "invalid"))
            return jsonify(error="Temporary reconciliation failure. Retry required."), 503
        return jsonify(status=result)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=8080, debug=False)
