# SIBC website

A server-rendered, responsive review build for Sarana International Buddhist
Center. English is primary; Sinhala and Khmer navigation/welcome alternatives
are explicitly **unreviewed drafts**, with remaining English content labeled.
Existing URLs redirect to their English equivalents. Historical events,
projects and financial information are retained with conspicuous dated-context
warnings, not promoted as current.

## Run locally

Python 3.11+ is required; Node is not required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python app.py
```

Open http://127.0.0.1:8080/. The server binds to loopback, runs without debug mode,
and is intended for local review only. All responses are marked noindex.
No hosting or accounts are provisioned. Do not expose this development server
publicly. A stable `SECRET_KEY` from a secret store/environment is required for
persistent browser sessions across restarts; absent one, local preview generates
an ephemeral key. Environment variables must be exported by the shell/runtime;
`.env` files are not automatically loaded or committed.

## Checks

```sh
.venv/bin/python -m pytest -q
# With the local server running:
.venv/bin/python -m playwright install chromium
SIBC_BROWSER_TESTS=1 .venv/bin/python -m pytest -q tests/test_browser.py
```

Unit/integration checks cover the full source sitemap and internal links, inert
legacy HTML, asset provenance, draft languages, amount validation, disabled/live
payment guards, CSRF, signed webhooks, duplicate/out-of-order delivery,
asynchronous outcomes, renewal failure, cancellation and safe error messages.
The browser check covers desktop/390px/320px reflow, images, keyboard navigation,
mobile menu and donation states. Screenshots are saved in ignored `test-results/`.
These checks do not replace manual screen-reader, translation or SIBC review.

## Content and assets

`content/legacy-pages.json` contains 123 sanitized source articles, retaining
original paths and source URLs. Core pages use edited source-based English copy;
original stories remain independently readable. The 131-entry source sitemap
is retained in `content/migration.json`; utility/feed routes have explicit
behavior, including a real 404. Original RSS/Atom feeds remain external links.
Remote scripts, donor-table integrations, embedded signup forms and tracking
are not carried forward. The existing Google Calendar can be loaded explicitly
or opened directly; its public feed was stale at inspection. No new event system
is implemented. Known closed signup and unavailable article links remain
historical references, not promises of current availability.

To explicitly recollect the public content and selected imagery:

```sh
.venv/bin/python scripts/collect_content.py --refresh
```

This uses only public GET requests, writes source-linked JSON and WebP images,
strips legacy executable HTML, omits private-phone/bank-number text, and never
submits forms. Review the resulting diff before accepting an upstream change.
It collects selected public image renditions rather than the complete archive.
`content/asset-sources.json` records source pages/URLs, dimensions, checksums,
processing and **unverified permission/subject-consent status** for all 19 images.
All images, religious-image treatment and descriptions need SIBC approval;
public availability is not a license. Historical gallery originals remain linked.

`content/locales.json` tracks source revision, draft status and absent reviewers.
Native speakers and religious/financial owners must review translations before
approval. No translation is represented as approved. Do not send private donor,
membership or child information to external translation services.

## Stripe: disabled by default, test-only by construction

The FSR repository's donation page and checkout endpoint were inspected as
interaction references. No FSR keys, product IDs, account settings, recipient
details or donor data were copied. Its success-query-string pattern was not
reused as payment verification.

The SIBC flow supports one-time/monthly/custom USD amounts through hosted
Checkout. The displayed presets and $1–$10,000 limits are **proposals requiring
finance approval**, not an existing SIBC policy. Stripe is disabled unless all
the SIBC-specific test settings in `.env.example` are supplied and the explicit
test-enable flag is set. Do not create an account to run this preview.
**Live keys, live Checkout Sessions and live webhook events are always rejected.**
The existing PayPal link remains available; confirm the recipient there.

Future authorized test setup requires:

- A separately owned/configured SIBC account, its account ID, a minimally
  privileged restricted **test** key and test webhook signing secret, stored
  server-side in the approved runtime's secret store/environment.
- `SECRET_KEY`, the correct `SIBC_SITE_URL`, persistent database storage,
  correct owner access and an approved HTTPS webhook address. Only loopback
  HTTP origins are permitted for local review.
- `SIBC_STRIPE_TEST_CHECKOUT_ENABLED=true`, only after the above are available.
  The application checks the API key's own account through Stripe before
  creating Sessions; access to a connected account alone does not establish
  ownership. Server-validated amounts never come from a client-supplied Price.

Routes: `POST /api/create-donation-checkout` and `POST /api/stripe/webhook`.
The first requires browser CSRF/attempt tokens, applies a durable per-IP-hash
rate limit and idempotent creation. The webhook verifies the raw-body signature
before processing, reconciles current Stripe state, and commits event IDs and
payment/invoice IDs transactionally to SQLite. Failed reconciliation returns
503 without marking the event processed, allowing Stripe retries.

Subscribe the future **test** endpoint to:

```text
checkout.session.completed
checkout.session.async_payment_succeeded
checkout.session.async_payment_failed
checkout.session.expired
invoice.paid
invoice.payment_failed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
charge.refunded
```

One-time payments are deduplicated by PaymentIntent; monthly gifts by invoice,
including checkout/invoice overlap. Only reconciled paid state counts as paid;
pending/failure never grants success. Return pages require the matching browser
attempt, a webhook-backed donation record and server retrieval, not just a
`status=success` query. They do not issue tax receipts. Records contain provider
IDs, amount/currency/status, not raw card data or donor names. SQLite data is
under ignored `instance/`; back it up and restrict filesystem access before any
external test exposure. This single-process preview ledger is not a claim of
production financial-accounting readiness.

No Stripe request, test payment or webhook registration was performed during
the credential-free build. Signed webhook/SDK behavior is tested with local
fixtures. Real test-mode end-to-end verification is still required once
authorized credentials exist. Before any future live implementation, SIBC must
approve recurring consent and cancellation/support, recipients, fees/refunds
(including recurring-payment refund reconciliation),
tax/receipt wording, privacy/retention, dispute handling and operational alerts;
complete provider-backed reconciliation and recovery tests. Live activation
requires a separate authorized change, not toggling a flag in this build.

## Architecture and review gates

`app.py` provides routing and page contexts; Jinja templates, shared CSS and
small progressive-enhancement JavaScript provide the core interface. Content,
locale variants, source manifests and payment reconciliation are separate.
Future approved modules can reuse this shell without changing existing URLs.
There is no AI assistant, new event functionality, visitor guide, school
functionality, FAQ system or announcement-management interface.

The preview's **Sources & review status** page lists remaining SIBC decisions:
content freshness/contact details, calendar/Dana ownership, image rights and
consent, translations, donation ownership/terms and operational payment setup.
No deployment or account creation is included.