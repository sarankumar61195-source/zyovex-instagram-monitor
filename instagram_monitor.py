"""Zyovex public Instagram Reel monitor.

This is a metadata-only GitHub Actions worker. It does not log in, use
session cookies, download media, or call Apify. Instagram may limit public
page responses; failures are written to the output instead of crashing the
workflow.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from urllib.parse import urlencode
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "reels.json")
USER_AGENT = (
    "Mozilla/5.0 (compatible; ZyovexPublicMonitor/1.0; "
    "+https://github.com/sarankumar61195-source/zyovex-instagram-monitor)"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def channel_urls() -> list[str]:
    raw = os.environ.get("CHANNEL_URLS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def canonical_channel_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    value = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return value + "/"


def fetch_html(url: str) -> str:
    api_key = os.environ.get("SCRAPFLY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SCRAPFLY_API_KEY is not configured")

    query = urlencode({
        "key": api_key,
        "url": canonical_channel_url(url),
        "asp": "true",
        "render_js": "true",
    })
    request = Request(
        "https://api.scrapfly.io/scrape?" + query,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urlopen(request, timeout=30) as response:  # nosec B310 - user-provided public URL
        body = response.read(10_000_000).decode("utf-8", errors="replace")

    payload = json.loads(body)
    result = payload.get("result") or {}
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Scrapfly returned no HTML content")
    return content


def decode_json_script(raw: str) -> object | None:
    try:
        return json.loads(html.unescape(raw))
    except (TypeError, ValueError):
        return None


def extract_json_ld(page: str) -> list[dict]:
    result: list[dict] = []
    for match in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page,
        flags=re.I | re.S,
    ):
        parsed = decode_json_script(match.strip())
        values = parsed if isinstance(parsed, list) else [parsed]
        result.extend(item for item in values if isinstance(item, dict))
    return result


def extract_reel_urls(page: str) -> list[str]:
    found = re.findall(
        r'https?://(?:www\.)?instagram\.com/(?:reel|p)/[A-Za-z0-9_-]+/?',
        page,
        flags=re.I,
    )
    result: list[str] = []
    seen: set[str] = set()
    for value in found:
        clean = html.unescape(value).split("?", 1)[0].rstrip("/") + "/"
        if clean.lower() not in seen:
            seen.add(clean.lower())
            result.append(clean)
    return result


def caption_from_page(page: str) -> str:
    match = re.search(
        r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        page,
        flags=re.I | re.S,
    )
    return html.unescape(match.group(1)).strip() if match else ""


def collect_channel(url: str) -> list[dict]:
    page = fetch_html(url)
    lowered = page.lower()
    if "challenge_required" in lowered or "challenge page" in lowered:
        raise RuntimeError("Instagram returned an anti-bot challenge page")
    if "login" in lowered and "instagram" in lowered and len(page) < 25000:
        raise RuntimeError("Instagram returned a login/blocked page")
    if len(page.strip()) < 1000:
        raise RuntimeError("Instagram returned an empty or incomplete page")
    fallback_caption = caption_from_page(page)
    items: list[dict] = []
    seen: set[str] = set()

    for record in extract_json_ld(page):
        record_url = str(record.get("url", ""))
        if "/reel/" not in record_url and "/p/" not in record_url:
            continue
        clean_url = record_url.split("?", 1)[0].rstrip("/") + "/"
        key = clean_url.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "url": clean_url,
                "caption": str(record.get("caption", fallback_caption) or ""),
                "postedAt": record.get("uploadDate") or record.get("datePublished") or "",
                "ownerUsername": "",
                "mediaType": "VideoObject" if record.get("video") else "ImageObject",
                "channelUrl": canonical_channel_url(url),
                "source": "GITHUB_PUBLIC_HTML",
            }
        )

    for reel_url in extract_reel_urls(page):
        if reel_url.lower() in seen:
            continue
        seen.add(reel_url.lower())
        items.append(
            {
                "url": reel_url,
                "caption": fallback_caption,
                "postedAt": "",
                "ownerUsername": "",
                "mediaType": "Video",
                "channelUrl": canonical_channel_url(url),
                "source": "GITHUB_PUBLIC_HTML",
            }
        )

    if not items:
        raise RuntimeError(
            "No public Reel data was present in the Instagram profile HTML"
        )

    return items


def main() -> int:
    urls = channel_urls()
    if not urls:
        print("CHANNEL_URLS is empty; no channels were processed.", file=sys.stderr)
        return 1

    collected: list[dict] = []
    errors: list[dict] = []
    for url in urls:
        try:
            collected.extend(collect_channel(url))
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            errors.append({"channelUrl": url, "error": str(error)})
        except Exception as error:  # defensive boundary for one channel
            errors.append({"channelUrl": url, "error": str(error)})

    unique: dict[str, dict] = {}
    for item in collected:
        unique[item["url"].lower()] = item

    if not unique and not errors:
        errors.append({
            "code": "NO_REELS_FOUND",
            "error": "No public Reel data was returned by Instagram",
        })

    output = {
        "success": bool(unique),
        "provider": "GITHUB_PUBLIC_HTML",
        "fetchedAt": now_iso(),
        "items": list(unique.values()),
        "errors": errors,
        "note": "Public HTML only; Instagram may return incomplete data or block requests.",
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"items": len(output["items"]), "errors": len(errors)}))
    return 0 if not errors or unique else 1


if __name__ == "__main__":
    raise SystemExit(main())
