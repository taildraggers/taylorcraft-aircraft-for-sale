"""Scraper for Taylorcraft aircraft listings on barnstormers.com.

Listings come from a Barnstormers advanced search on the ad headline
"Taylorcraft". Only whole-aircraft-for-sale listings are published: each
ad's title must name a Taylorcraft and no other airplane make, and titles
that look like parts/accessories/services/raffles are dropped. Surviving
titles are rewritten to a canonical "YEAR Taylorcraft MODEL" form so every
listing follows the same format.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .common import (
    MIN_AIRCRAFT_PRICE,
    Listing,
    extract_date,
    extract_location,
    extract_price,
    fetch,
    format_aircraft_title,
    price_value,
)

SITE_NAME = "Barnstormers.com"
BASE = "https://www.barnstormers.com"
MAKE = "Taylorcraft"

# Barnstormers advanced search for ads whose headline contains "Taylorcraft".
SEARCH_URLS = [
    f"{BASE}/cat_search.php?headline=Taylorcraft&body=&part_num=&mfg=&model="
    "&user__profile__company=&user__last_name=&user__first_name="
    "&user__profile__country=&specialcase__state=&user__profile__city="
    "&user__profile__uzip=&specialcase__phone=&user__email=&my_cats__name="
    "&price__gte=&price__lte=&search_type=advanced&keyword=",
]

MAX_PAGES = 15
LISTING_LINK_RE = re.compile(r"^/classified-(\d+)-(.+)\.html$")
GENERIC_SITE_TITLE_SNIPPET = "barnstormers.com find aircraft"

_TAYLORCRAFT_RE = re.compile(r"taylor\s*-?\s*craft|\bt\s?-?\s?craft", re.IGNORECASE)
# Model codes, most specific first, each with any separator ("BC-12D",
# "BC12D", "BC 12 D" - URL slugs read hyphens as spaces). Each rule maps a
# match to its canonical name; the BC-12 keeps whatever suffix follows it
# ("BC-12D", "BC-12D1", "BC-12-65").
_SEP = r"\s?-?\s?"
_MODEL_RULES = [
    (re.compile(rf"\bbc{_SEP}12{_SEP}(d\d?|65)?(?![\da-z])", re.IGNORECASE),
     lambda m: "BC-12" + (("D" + m.group(1)[1:]) if m.group(1) and m.group(1).lower().startswith("d") else (f"-{m.group(1)}" if m.group(1) else ""))),
    (re.compile(rf"\bbc{_SEP}65(?!\d)", re.IGNORECASE), lambda m: "BC-65"),
    (re.compile(r"\bbcs(?:\s?-?\s?(12d?|65))?\b", re.IGNORECASE), lambda m: "BCS" + (f"-{m.group(1).upper()}" if m.group(1) else "")),
    (re.compile(rf"\bdc{_SEP}65(?!\d)", re.IGNORECASE), lambda m: "DC-65"),
    (re.compile(rf"\bb([lf]){_SEP}(65)?(?![\da-z])", re.IGNORECASE), lambda m: "B" + m.group(1).upper() + (f"-{m.group(2)}" if m.group(2) else "")),
    (re.compile(rf"\bl{_SEP}2([a-m])?(?![\da-z])", re.IGNORECASE), lambda m: "L-2" + (m.group(1).upper() if m.group(1) else "")),
    (re.compile(rf"\bf{_SEP}(19|21|22)([abc])?(?![\da-z])", re.IGNORECASE), lambda m: f"F-{m.group(1)}" + (m.group(2).upper() if m.group(2) else "") + (" Sportsman" if m.group(1) == "19" else "")),
    (re.compile(r"\bplus\s?-?\s?([cd])\b", re.IGNORECASE), lambda m: f"Plus {m.group(1).upper()}"),
    (re.compile(r"\bsportsman\b", re.IGNORECASE), lambda m: "F-19 Sportsman"),
    (re.compile(r"ranch\s*wagon", re.IGNORECASE), lambda m: "Ranch Wagon"),
    (re.compile(r"\bbc\b", re.IGNORECASE), lambda m: "BC"),
]
# Other makes. A "Taylorcraft" title naming one of these is a comparison or a
# mixed parts ad, and is dropped.
_OTHER_MAKE_RE = re.compile(
    r"\bcub\b|piper|cessna|aeronca|\bchamp\b|luscombe|ercoupe|stinson|citabria",
    re.IGNORECASE,
)


def _extract_model(title: str) -> tuple[str, str] | None:
    if not _TAYLORCRAFT_RE.search(title) or _OTHER_MAKE_RE.search(title):
        return None
    for pattern, canonical in _MODEL_RULES:
        match = pattern.search(title)
        if match:
            return MAKE, canonical(match)
    return MAKE, ""


def _title_from_url(url: str) -> str:
    """Listing pages share a generic <title>/<h1>, but the URL slug is the ad's own title."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    match = LISTING_LINK_RE.match("/" + slug)
    if not match:
        return unquote(slug)
    return unquote(match.group(2)).replace("-", " ").strip()


def _find_listing_links(html: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = urlparse(urljoin(BASE, a["href"])).path
        if LISTING_LINK_RE.match(href):
            links.add(urljoin(BASE, href))
    return links


def _page_url(search_url: str, page: int) -> str:
    """Build a search results page's URL directly by setting its page=<n>
    query parameter, rather than hunting for "Next" links - Barnstormers'
    pager renders as bare page-number buttons with no "Next" text or
    rel="next" for a link-following heuristic to find.
    """
    if page <= 1:
        return search_url
    parts = urlparse(search_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    query.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _debug_dump_hrefs(html: str, limit: int = 25) -> None:
    soup = BeautifulSoup(html, "lxml")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    interesting = [h for h in hrefs if "classified" in h.lower() or "taylorcraft" in h.lower()]
    sample = interesting[:limit] or hrefs[:limit]
    print(f"  [debug] {len(hrefs)} total <a href> on page; sample: {sample}")


def _parse_detail_page(url: str, html: str) -> Listing | None:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        title = re.sub(r"\s*[\|\-]\s*Barnstormers.*$", "", title, flags=re.IGNORECASE).strip()
    if not title or GENERIC_SITE_TITLE_SNIPPET in title.lower():
        title = _title_from_url(url)
    if not title:
        return None

    text = soup.get_text(" ", strip=True)

    formatted_title = format_aircraft_title(title, text, _extract_model)
    if not formatted_title:
        return None
    title = formatted_title

    price = extract_price(text)
    value = price_value(price)
    if value is not None and value < MIN_AIRCRAFT_PRICE:
        return None
    location = extract_location(text)
    date_posted = extract_date(text)

    return Listing(
        title=title,
        price=price,
        location=location,
        date_posted=date_posted,
        site=SITE_NAME,
        url=url,
    )


def scrape() -> list[Listing]:
    print(f"[{SITE_NAME}] starting scrape")
    all_links: set[str] = set()

    for search_url in SEARCH_URLS:
        seen_this_search: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            url = _page_url(search_url, page)
            html = fetch(url)
            if not html:
                break
            links = _find_listing_links(html)
            new_links = links - seen_this_search
            print(f"  [search page {page}]: {len(links)} links ({len(new_links)} new)")
            if page == 1 and not links:
                _debug_dump_hrefs(html)
            seen_this_search |= links
            if not new_links:
                break
        all_links |= seen_this_search

    print(f"[{SITE_NAME}] {len(all_links)} unique listing URLs found")

    listings: list[Listing] = []
    for url in sorted(all_links):
        html = fetch(url)
        if not html:
            continue
        listing = _parse_detail_page(url, html)
        if listing:
            listings.append(listing)

    print(f"[{SITE_NAME}] parsed {len(listings)} listings")
    return listings
