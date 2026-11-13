import json
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import pytest

from app import create_app, PAGES


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test-secret", "DATABASE": str(tmp_path / "test.sqlite3")})


def test_core_and_legacy_routes_preserve_content(app):
    client = app.test_client()
    migration = json.loads(Path("content/migration.json").read_text())
    for url in migration["sitemap_urls"]:
        path = urlparse(url).path
        if path in ("/feed.xml", "/atom.xml"):
            assert client.get(path).location == "https://sibc-tukwila.org" + path
            continue
        response = client.get(path, follow_redirects=True)
        if path == "/404.html":
            assert response.status_code == 404
        else:
            assert response.status_code == 200, path
    for path in PAGES:
        assert client.get("/en" + path).status_code == 200, path


def test_archive_is_not_upcoming(app):
    client = app.test_client()
    upcoming = client.get("/en/events/upcoming/").text
    assert "No upcoming events have been verified" in upcoming
    assert "Blessings for the New Year - 2025" not in upcoming
    historical = client.get("/en/events/2025/january/blessings/").text
    assert "Historical source content" in historical
    assert "Wednesday, 1st of January 2025" in historical
    campaigns = client.get("/en/donations/campaigns/").text
    assert "No campaign is presented as currently active" in campaigns


@pytest.mark.parametrize("locale,native", [("si", "මුල් පිටුව"), ("km", "ទំព័រដើម")])
def test_draft_locales_are_honest(app, locale, native):
    response = app.test_client().get(f"/{locale}/")
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.html["lang"] == locale
    assert "not approved" in response.text
    assert "Other content remains in English" in response.text
    assert native in response.text
    assert soup.select_one(".welcome-band")["lang"] == "en"
    assert soup.select_one(".language-nav a[lang=en]")["href"] == "/en/"


def test_checkout_disabled_without_configuration_and_for_live_key(app):
    client = app.test_client()
    soup = BeautifulSoup(client.get("/en/donations/").text, "html.parser")
    assert soup.select_one(".checkout-button").has_attr("disabled")
    assert soup.select_one("a[href='https://www.paypal.me/SIBC']")
    with client.session_transaction() as state:
        csrf = state["csrf"]
    response = client.post("/api/create-donation-checkout", json={"amount": "25"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 503
    app.config.update(SIBC_STRIPE_SECRET_KEY="sk_live_rejected", SIBC_STRIPE_TEST_CHECKOUT_ENABLED="true")
    assert client.post("/api/create-donation-checkout", json={}, headers={"X-CSRF-Token": csrf}).status_code == 503
    assert client.post("/api/stripe/webhook").status_code == 503


def test_return_url_does_not_claim_success(app):
    result = app.test_client().get("/en/donations/return/?status=success&session_id=cs_untrusted")
    assert "Payment is not confirmed" in result.text
    assert "Test payment verified" not in result.text


def test_imported_html_is_inert_and_local_images_have_provenance(app):
    for page in PAGES.values():
        soup = BeautifulSoup(page["html"], "html.parser")
        assert not soup.select("script,iframe,form,img,style"), page["path"]
        for element in soup.find_all(True):
            assert not any(key.startswith("on") or key == "style" for key in element.attrs)
        for anchor in soup.select("a[href]"):
            assert urlparse(anchor["href"]).scheme in ("", "http", "https", "mailto", "tel")
    assets = json.loads(Path("content/asset-sources.json").read_text())
    assert len(assets) >= 19
    for asset in assets:
        assert Path(asset["file"].lstrip("/")).is_file()
        assert asset["source_url"].startswith("https://")
        assert "review required" in asset["permission"]
        if asset["source_url"].startswith("https://sibc-tukwila.org/"):
            result = app.test_client().get(urlparse(asset["source_url"]).path, follow_redirects=True)
            assert result.status_code == 200
            assert result.mimetype == "image/webp"
    assert "Account number 652" not in PAGES["/donations/campaigns/wall_project/"]["html"]


def test_navigation_and_page_links_resolve(app):
    client = app.test_client()
    paths = ["/en" + path for path in PAGES] + ["/en/", "/en/review/"]
    targets = set()
    for path in paths:
        soup = BeautifulSoup(client.get(path).text, "html.parser")
        for anchor in soup.select("a[href]"):
            target = anchor["href"].split("#")[0]
            if target.startswith("/") and not target.startswith("//"):
                targets.add(target)
    for target in targets:
        if target in ("/feed.xml", "/atom.xml"):
            assert client.get(target).status_code == 302
            continue
        assert client.get(target, follow_redirects=True).status_code < 400, target


def test_security_headers_and_csrf(app):
    client = app.test_client()
    response = client.get("/en/")
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert client.post("/api/create-donation-checkout", json={"amount": "25"}).status_code == 403
    assert client.get("/does-not-exist/").status_code == 404
