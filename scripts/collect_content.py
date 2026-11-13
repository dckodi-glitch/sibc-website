"""Rebuild the public-source inventory without executing legacy page scripts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://sibc-tukwila.org"
ALLOWED = {
    "p", "h2", "h3", "h4", "ul", "ol", "li", "a", "strong", "em", "br",
    "table", "thead", "tbody", "tr", "th", "td", "blockquote", "div", "span",
    "figure", "figcaption",
}


def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "SIBC-content-migration/1.0"}), timeout=30) as response:
        return response.read(12_000_000)


def collect_page(url):
    path = urlparse(url).path
    if not path.endswith("/") or path == "/":
        return None
    soup = BeautifulSoup(fetch(url), "html5lib")
    title = soup.title.get_text(" ", strip=True) if soup.title else path
    article = soup.select_one(".row.t30 > .medium-12") or soup.select_one("article")
    if article is None:
        return None
    sources = []
    for image in article.find_all("img"):
        src = image.get("data-src") or image.get("src")
        if src:
            sources.append({"url": urljoin(BASE, src), "alt": image.get("alt", "")})
    for element in article.select("script, style, form, iframe, .pa-embed-player, #fr_table, #fr_chart"):
        element.decompose()
    for element in list(article.find_all(True)):
        if element.parent is None:
            continue
        if element.name == "header":
            element.decompose()
        elif element.name == "img":
            element.decompose()
        elif element.name == "h1":
            element.name = "h2"
        elif element.name not in ALLOWED:
            element.unwrap()
    for element in article.find_all(True):
        href = element.get("href")
        element.attrs = {}
        if element.name == "a" and href:
            target = urljoin(BASE + path, href)
            parsed = urlparse(target)
            if parsed.scheme in ("https", "http", "mailto", "tel"):
                element["href"] = parsed.path + ("#" + parsed.fragment if parsed.fragment else "") if parsed.netloc == "sibc-tukwila.org" else target
        if element.name == "th":
            element["scope"] = "col"
    html = article.decode_contents()
    html = re.sub(r"(?:Account number|Routing number)\s*[\d -]+", "[Bank details omitted pending SIBC review]", html, flags=re.I)
    html = re.sub(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b", "[Personal phone omitted pending review]", html)
    historical = bool(re.search(r"/(?:events|news)/\d{4}/", path)) or path.startswith("/donations/")
    return {
        "path": path, "title": title, "source": url, "html": html,
        "historical": historical, "review": "SIBC content review required",
        "images": sources,
    }


def collect_image(url, name, source_page):
    raw = fetch(url)
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    original_size = image.size
    image.thumbnail((1800, 1800))
    target = ROOT / "static" / "images" / f"{name}.webp"
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "WEBP", quality=86)
    return {
        "file": f"/static/images/{name}.webp", "source_url": url,
        "source_page": source_page, "original_dimensions": list(original_size),
        "dimensions": list(image.size), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "permission": "unverified - SIBC permission and subject-consent review required",
        "review": "Review crop, accuracy, alt text and any identifiable people before publication",
        "processing": "EXIF removed; resized and converted to WebP for local review",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="Explicitly fetch public source pages and selected images")
    args = parser.parse_args()
    if not args.refresh:
        parser.error("Pass --refresh to collect public content; this never runs during application requests.")
    root = ET.fromstring(fetch(BASE + "/sitemap.xml"))
    urls = [item.text for item in root.findall(".//{*}loc")]
    with ThreadPoolExecutor(max_workers=4) as executor:
        pages = [page for page in executor.map(collect_page, urls) if page]
    media = next(page for page in pages if page["path"] == "/about/media/")
    assets = [collect_image(BASE + "/images/main-banner.jpg", "temple-banner", BASE + "/")]
    for index, image in enumerate(media["images"][:5], 1):
        assets.append(collect_image(image["url"], f"temple-{index}", media["source"]))
    image_map = {asset["source_url"]: asset["file"] for asset in assets}
    for page in pages:
        for image in page["images"]:
            if urlparse(image["url"]).netloc == "sibc-tukwila.org" and image["url"] not in image_map:
                name = Path(urlparse(image["url"]).path).stem
                asset = collect_image(image["url"], name, page["source"])
                assets.append(asset)
                image_map[image["url"]] = asset["file"]
            if image["url"] in image_map:
                image["local"] = image_map[image["url"]]
    content = ROOT / "content"
    content.mkdir(exist_ok=True)
    (content / "legacy-pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2) + "\n")
    (content / "asset-sources.json").write_text(json.dumps(assets, indent=2) + "\n")
    (content / "migration.json").write_text(json.dumps({
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE, "sitemap_urls": urls, "migrated_articles": len(pages),
        "images_collected": len(assets), "external_galleries": [
            "https://photos.app.goo.gl/VSdYhdY1dVkus8Ut5",
            "https://photos.app.goo.gl/m6tZwVKPqdemoErd7",
        ],
        "notes": [
            "Remote scripts, embedded forms and dynamic donor tables are not executed or migrated.",
            "Original article links remain available; historical signup links are not current invitations.",
            "Public accessibility is not permission. All images and factual content need SIBC review.",
            "Selected public renditions collected, not the complete historical photo archive.",
        ],
    }, indent=2) + "\n")
    print(f"Collected {len(pages)} articles and {len(assets)} source-recorded images.")


if __name__ == "__main__":
    main()
