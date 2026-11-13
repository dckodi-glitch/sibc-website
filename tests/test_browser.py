import os
from pathlib import Path

from playwright.sync_api import sync_playwright
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SIBC_BROWSER_TESTS") != "1", reason="Run explicitly against the local review server with SIBC_BROWSER_TESTS=1.")
BASE = "http://127.0.0.1:8080"


def test_desktop_mobile_keyboard_and_donation_states():
    output = Path("test-results")
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        for width, label in ((1440, "desktop"), (390, "mobile"), (320, "small-mobile")):
            page.set_viewport_size({"width": width, "height": 900})
            for path in ("/en/", "/en/donations/", "/en/about/", "/en/contact/", "/en/events/calendar/", "/en/events/2025/january/blessings/", "/en/donations/campaigns/wall_project/", "/si/", "/km/"):
                page.goto(BASE + path)
                page.wait_for_load_state("networkidle")
                page.locator("img").evaluate_all("(images) => images.forEach(i => { i.loading = 'eager'; })")
                page.evaluate("async () => { await Promise.all([...document.images].map(i => i.decode().catch(() => undefined))); }")
                assert page.locator("h1").count() == 1, path
                assert page.locator("main").count() == 1
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, path)
                missing = page.locator("img").evaluate_all("(images) => images.filter(i => !i.hasAttribute('alt') || !i.complete || i.naturalWidth === 0).length")
                assert missing == 0, (path, missing)
                if width in (1440, 390) and path in ("/en/", "/en/donations/"):
                    name = "home" if path == "/en/" else "donations"
                    page.screenshot(path=str(output / f"{label}-{name}.png"), full_page=True, animations="disabled")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(BASE + "/en/")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.textContent") == "Skip to content"
        page.keyboard.press("Enter")
        assert page.evaluate("document.activeElement.id") == "main"
        menu = page.get_by_role("button", name="Menu")
        menu.click()
        assert menu.get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")
        assert menu.get_attribute("aria-expanded") == "false"
        page.goto(BASE + "/en/donations/")
        assert page.get_by_role("button", name="Stripe checkout disabled").is_disabled()
        page.get_by_label("Monthly", exact=True).check()
        assert page.locator("#monthly-terms").is_visible()
        assert "every month" in page.locator("#donation-summary").inner_text()
        page.get_by_label("Custom", exact=True).check()
        page.get_by_label("Donation amount in USD", exact=True).fill("42.19")
        assert "$42.19 USD" in page.locator("#donation-summary").inner_text()
        page.get_by_label("One-time", exact=True).check()
        assert not page.locator("#monthly-terms").is_visible()
        page.goto(BASE + "/en/events/calendar/")
        assert page.locator("iframe").count() == 0
        assert failures == []
        context.close()
        browser.close()
