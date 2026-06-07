from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import requests
    from bs4 import BeautifulSoup
except ModuleNotFoundError as import_error:  # Allows --check-config before dependencies are installed.
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]
    OPTIONAL_IMPORT_ERROR = import_error
else:
    OPTIONAL_IMPORT_ERROR = None


SHEET_ID_DEFAULT = "1q7mjRmjI8ywrSXe1OU6oZ2jNOib0KHNoEK7i8rUH0vg"

MASTER_SHEET = "Master Sheet"
ROSTER_SHEET = "Roster Checklist"
CHANGE_LOG_SHEET = "Change Log"
RUN_LOG_SHEET = "Run Log"
ERRORS_SHEET = "Errors"

MASTER_COLUMNS = [
    "Roaster",
    "Name",
    "Roast Profile",
    "Flavour Notes",
    "Origin / Location",
    "Varietal / Species",
    "Process",
    "Elevation",
    "Price",
    "Product Link",
]
ROSTER_COLUMNS = [
    "Enabled",
    "Roaster",
    "Source URL",
    "Platform",
    "Exclude Keywords",
    "Example Product Link",
    "Last Checked",
    "Last Status",
    "Notes",
]
CHANGE_LOG_COLUMNS = [
    "Timestamp",
    "Run ID",
    "Roaster",
    "Change Type",
    "Product Name",
    "Product Link",
    "Field Changed",
    "Old Value",
    "New Value",
    "Notes",
]
RUN_LOG_COLUMNS = [
    "Timestamp",
    "Run ID",
    "Started At",
    "Finished At",
    "Status",
    "Roasters Checked",
    "Products Scanned",
    "New Products Added",
    "Products Updated",
    "Errors",
    "Notification Sent",
    "Notes",
]
ERROR_COLUMNS = [
    "Timestamp",
    "Run ID",
    "Roaster",
    "Source URL",
    "Error Type",
    "Error Message",
    "Action Needed",
    "Resolved",
]

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
HTTP_TIMEOUT = 30
GOOGLE_API_MAX_ATTEMPTS = 5
GOOGLE_WRITE_CHUNK_SIZE = 100
MAX_PRODUCTS_PER_ROASTER = 250

GLOBAL_EXCLUDE_KEYWORDS = [
    "equipment",
    "accessory",
    "accessories",
    "merch",
    "merchandise",
    "gift card",
    "subscription",
    "sampler",
    "sample pack",
    "tester",
    "drip bag",
    "drip filter",
    "filter paper",
    "pourover pack",
    "pourtable pourover",
    "bulk",
    "wholesale",
    "matcha",
    "gift box",
    "coffee maker",
    "coffee mug",
]

SOURCE_URL_REPLACEMENTS = {
    "https://naivo.in/product-category/coffee": "https://naivo.in/shop/",
    "https://redsirocco.com/product-category/coffee": "https://redsirocco.com/product-sitemap.xml",
}

READER_FALLBACK_HOSTS = {"redsirocco.com"}


@dataclass
class RosterEntry:
    row_number: int
    enabled: bool
    roaster: str
    source_url: str
    platform: str
    exclude_keywords: list[str]
    example_product_link: str
    notes: str


@dataclass
class Product:
    roaster: str
    name: str
    roast_profile: str
    flavour_notes: str
    origin_location: str
    varietal_species: str
    process: str
    elevation: str
    price: str
    product_link: str
    raw_search_text: str = ""

    def as_master_row(self) -> list[str]:
        return [
            self.roaster,
            self.name,
            self.roast_profile,
            self.flavour_notes,
            self.origin_location,
            self.varietal_species,
            self.process,
            self.elevation,
            self.price,
            self.product_link,
        ]


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def timestamp() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")


def run_id() -> str:
    return now_ist().strftime("%Y%m%d-%H%M%S")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def clean_cell(value: Any, max_len: int = 500) -> str:
    text = clean_text(value)
    text = re.sub(r"\s*\|\s*", " | ", text)
    text = re.sub(r"\s+", " ", text).strip(" :-–|,")
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def strip_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return clean_text(soup.get_text("\n"))


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(urljoin(url, url))
    path = re.sub(r"/+$", "", parsed.path or "")
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))


def replacement_source_url(url: str) -> str:
    return SOURCE_URL_REPLACEMENTS.get(canonical_url(url), url)


def reader_fallback_url(url: str) -> str:
    return "https://r.jina.ai/http://" + url


def product_key(url: str) -> str:
    return canonical_url(url).lower()


def sheet_range(sheet_name: str, a1: str) -> str:
    safe = sheet_name.replace("'", "''")
    return f"'{safe}'!{a1}"


def column_letter(index: int) -> str:
    output = ""
    while index:
        index, rem = divmod(index - 1, 26)
        output = chr(65 + rem) + output
    return output


def parse_enabled(value: str) -> bool:
    return clean_cell(value).lower() in {"true", "yes", "y", "1", "enabled"}


def split_keywords(value: str) -> list[str]:
    parts = re.split(r"[,\n]", value or "")
    keywords = [clean_cell(part).lower() for part in parts if clean_cell(part)]
    for keyword in GLOBAL_EXCLUDE_KEYWORDS:
        if keyword not in keywords:
            keywords.append(keyword)
    return keywords


def get_nested_values(data: Any) -> list[Any]:
    if isinstance(data, list):
        out: list[Any] = []
        for item in data:
            out.extend(get_nested_values(item))
        return out
    if isinstance(data, dict):
        if "@graph" in data:
            return [data] + get_nested_values(data["@graph"])
        return [data]
    return []


def labels_pattern(labels: list[str]) -> str:
    escaped = [re.escape(label) for label in labels]
    return r"(?:" + "|".join(escaped) + r")"


FIELD_LABELS = {
    "roast_profile": [
        "roast profile",
        "roast level",
        "roast",
        "recommended roast",
        "recommended for",
        "profile",
        "brew profile",
        "suitable for",
    ],
    "flavour_notes": [
        "flavour notes",
        "flavor notes",
        "tasting notes",
        "taste notes",
        "cup notes",
        "cupping notes",
        "sensory notes",
    ],
    "origin_location": [
        "origin / location",
        "origin",
        "region",
        "location",
        "estate",
        "farm",
        "producer",
        "producers",
    ],
    "varietal_species": [
        "varietal / species",
        "varietal",
        "variety",
        "varieties",
        "species",
        "cultivar",
    ],
    "process": [
        "process",
        "processing",
        "processing method",
        "method",
    ],
    "elevation": [
        "elevation",
        "altitude",
        "height",
        "masl",
    ],
}

ALL_FIELD_LABELS = sorted({label for labels in FIELD_LABELS.values() for label in labels}, key=len, reverse=True)


def extract_by_labels(text: str, labels: list[str], max_len: int = 220) -> str:
    if not text:
        return ""
    lines = [clean_cell(line, 300) for line in text.splitlines()]
    lines = [line for line in lines if line]
    label_re = re.compile(rf"^\s*{labels_pattern(labels)}\s*[:\-–|]?\s*(.*)$", re.I)
    any_label_re = re.compile(rf"^\s*{labels_pattern(ALL_FIELD_LABELS)}\s*[:\-–|]?\s*", re.I)

    for index, line in enumerate(lines):
        match = label_re.match(line)
        if not match:
            continue
        value = clean_cell(match.group(1), max_len)
        if value and value.lower() not in {label.lower() for label in labels}:
            return value
        collected: list[str] = []
        for next_line in lines[index + 1 : index + 5]:
            if any_label_re.match(next_line):
                break
            if next_line.lower() in {"add to cart", "buy now", "select options", "quick view"}:
                break
            collected.append(next_line)
            if len("; ".join(collected)) >= max_len:
                break
        if collected:
            return clean_cell("; ".join(collected), max_len)

    compact = "\n".join(lines)
    next_label = labels_pattern(ALL_FIELD_LABELS)
    for label in labels:
        pattern = re.compile(
            rf"\b{re.escape(label)}\b\s*[:\-–|]\s*(.+?)(?=\n\s*{next_label}\b\s*[:\-–|]|\n{{2,}}|$)",
            re.I | re.S,
        )
        match = pattern.search(compact)
        if match:
            return clean_cell(match.group(1), max_len)
    return ""


def extract_price_from_text(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"Price\s*[:\-]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return format_inr(match.group(1))
    return ""


def format_inr(value: Any) -> str:
    text = clean_cell(value)
    if not text:
        return ""
    match = re.search(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", text)
    if not match:
        return text
    amount = match.group(0).replace(",", "")
    if amount.endswith(".00"):
        amount = amount[:-3]
    return f"INR {amount}"


def extract_price_from_offers(offers: Any) -> str:
    offer_items = offers if isinstance(offers, list) else [offers]
    prices: list[str] = []
    for offer in offer_items:
        if isinstance(offer, dict):
            price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
            if price:
                prices.append(format_inr(price))
    return prices[0] if prices else ""


def map_property_to_field(name: str, value: str, fields: dict[str, str]) -> None:
    normalized = clean_cell(name).lower().replace("pa_", "").replace("-", " ").replace("_", " ")
    cleaned_value = clean_cell(value, 220)
    if not cleaned_value:
        return
    if "flavour" in normalized or "flavor" in normalized or "tasting" in normalized or "cup note" in normalized:
        fields.setdefault("flavour_notes", cleaned_value)
    elif "roast" in normalized or "recommended" in normalized or "brew" in normalized:
        fields.setdefault("roast_profile", cleaned_value)
    elif "origin" in normalized or "region" in normalized or "location" in normalized or "estate" in normalized or "producer" in normalized:
        fields.setdefault("origin_location", cleaned_value)
    elif "variet" in normalized or "variety" in normalized or "species" in normalized or "cultivar" in normalized:
        fields.setdefault("varietal_species", cleaned_value)
    elif "process" in normalized or "processing" in normalized:
        fields.setdefault("process", cleaned_value)
    elif "elevation" in normalized or "altitude" in normalized or "masl" in normalized:
        fields.setdefault("elevation", cleaned_value)


def extract_additional_properties(product_json: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    properties = product_json.get("additionalProperty") or product_json.get("additionalProperties") or []
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name") or prop.get("propertyID") or prop.get("identifier") or ""
        value = prop.get("value") or prop.get("description") or prop.get("text") or ""
        map_property_to_field(name, value, fields)
    return fields


def extract_roast_profile(text: str, title: str, tags: list[str]) -> str:
    labelled = extract_by_labels(text, FIELD_LABELS["roast_profile"])
    if labelled:
        return labelled

    candidates = [title, *tags]
    phrases = [
        "Ultra Light Roast",
        "Light Medium Roast",
        "Medium Light Roast",
        "Medium Dark Roast",
        "Medium-Dark Roast",
        "Light Roast",
        "Medium Roast",
        "Dark Roast",
        "Espresso Roast",
        "Filter Roast",
        "Omni Roast",
        "Filter / Espresso",
        "Espresso | Filter",
        "Filter",
        "Espresso",
    ]
    for source in candidates:
        for phrase in phrases:
            if re.search(rf"\b{re.escape(phrase)}\b", source or "", re.I):
                return phrase
    return ""


def extract_product_fields(text: str, title: str, tags: list[str], jsonld_product: dict[str, Any] | None = None) -> dict[str, str]:
    fields = extract_additional_properties(jsonld_product or {})
    fields.setdefault("roast_profile", extract_roast_profile(text, title, tags))
    for key in ["flavour_notes", "origin_location", "varietal_species", "process", "elevation"]:
        if not fields.get(key):
            fields[key] = extract_by_labels(text, FIELD_LABELS[key])
    return {key: clean_cell(value) for key, value in fields.items()}


class CoffeeScraper:
    def __init__(self) -> None:
        if requests is None or BeautifulSoup is None:
            raise RuntimeError("Missing Python dependencies. Run: pip install -r requirements.txt") from OPTIONAL_IMPORT_ERROR
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def fetch(self, url: str) -> requests.Response:
        response = self.session.get(url, timeout=HTTP_TIMEOUT)
        host = urlparse(url).netloc.lower()
        if response.status_code in {403, 429} and host in READER_FALLBACK_HOSTS:
            response = self.session.get(reader_fallback_url(url), timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return response

    def scrape(self, entry: RosterEntry) -> list[Product]:
        source_url = replacement_source_url(entry.source_url)
        if source_url != entry.source_url:
            entry = replace(entry, source_url=source_url)
        source = entry.source_url.lower()
        if source.endswith("products.json") or "products.json" in source:
            products = self.scrape_shopify(entry)
        else:
            products = self.scrape_product_pages(entry)

        deduped: dict[str, Product] = {}
        for product in products:
            if not product.name or not product.product_link:
                continue
            if self.should_exclude(product, entry.exclude_keywords):
                continue
            deduped.setdefault(product_key(product.product_link), product)
        return list(deduped.values())

    def should_exclude(self, product: Product, keywords: list[str]) -> bool:
        haystack = " ".join([product.name, product.product_link, product.raw_search_text]).lower()
        return any(keyword and keyword in haystack for keyword in keywords)

    def scrape_shopify(self, entry: RosterEntry) -> list[Product]:
        response = self.fetch(entry.source_url)
        data = response.json()
        products_data = data.get("products", []) if isinstance(data, dict) else []
        base_url = f"{urlparse(entry.source_url).scheme}://{urlparse(entry.source_url).netloc}"
        products: list[Product] = []
        for item in products_data[:MAX_PRODUCTS_PER_ROASTER]:
            title = clean_cell(item.get("title"))
            handle = clean_cell(item.get("handle"))
            if not title or not handle:
                continue
            body_text = strip_html(item.get("body_html") or "")
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [part.strip() for part in tags.split(",") if part.strip()]
            variants = item.get("variants") or []
            prices = [format_inr(variant.get("price")) for variant in variants if isinstance(variant, dict) and variant.get("price")]
            price = prices[0] if prices else ""
            product_type = clean_cell(item.get("product_type"))
            raw_search = " ".join([product_type, " ".join(tags)])
            product_link = canonical_url(urljoin(base_url, f"/products/{handle}"))
            source_text = "\n".join([title, body_text, "Tags: " + ", ".join(tags), product_type])
            fields = extract_product_fields(source_text, title, tags)
            products.append(
                Product(
                    roaster=entry.roaster,
                    name=title,
                    roast_profile=fields.get("roast_profile", ""),
                    flavour_notes=fields.get("flavour_notes", ""),
                    origin_location=fields.get("origin_location", ""),
                    varietal_species=fields.get("varietal_species", ""),
                    process=fields.get("process", ""),
                    elevation=fields.get("elevation", ""),
                    price=price,
                    product_link=product_link,
                    raw_search_text=raw_search,
                )
            )
        return products

    def scrape_product_pages(self, entry: RosterEntry) -> list[Product]:
        links = self.discover_product_links(entry.source_url)
        products: list[Product] = []
        for link in links[:MAX_PRODUCTS_PER_ROASTER]:
            try:
                products.append(self.parse_product_page(entry, link))
            except Exception as exc:
                print(f"Skipping product page {link}: {exc}", file=sys.stderr)
        return products

    def discover_product_links(self, source_url: str) -> list[str]:
        response = self.fetch(source_url)
        soup = BeautifulSoup(response.text, "html.parser")
        links: list[str] = []
        source_host = urlparse(source_url).netloc.lower()
        product_patterns = [
            "/product-page/",
            "/products/",
            "/product/",
        ]
        for loc in soup.find_all("loc"):
            absolute = canonical_url(clean_cell(loc.get_text()))
            parsed = urlparse(absolute)
            if parsed.netloc.lower() == source_host and any(pattern in parsed.path for pattern in product_patterns):
                links.append(absolute)
        for match in re.findall(r"https?://[^\s\])<>\"']+", response.text):
            absolute = canonical_url(match.rstrip(".,;"))
            parsed = urlparse(absolute)
            if parsed.netloc.lower() == source_host and any(pattern in parsed.path for pattern in product_patterns):
                links.append(absolute)
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href") or ""
            absolute = canonical_url(urljoin(source_url, href))
            parsed = urlparse(absolute)
            if parsed.netloc.lower() != source_host:
                continue
            if any(pattern in parsed.path for pattern in product_patterns):
                if not absolute.lower().endswith("products.json"):
                    links.append(absolute)
        seen: set[str] = set()
        unique_links: list[str] = []
        for link in links:
            key = product_key(link)
            if key not in seen:
                seen.add(key)
                unique_links.append(link)
        return unique_links

    def parse_product_page(self, entry: RosterEntry, url: str) -> Product:
        response = self.fetch(url)
        soup = BeautifulSoup(response.text, "html.parser")
        jsonld_product = self.find_jsonld_product(soup)
        canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
        product_link = canonical_url(canonical.get("href") if canonical and canonical.get("href") else url)

        title = ""
        if jsonld_product:
            title = clean_cell(jsonld_product.get("name"))
        if not title:
            h1 = soup.find("h1")
            title = clean_cell(h1.get_text(" ")) if h1 else ""
        if not title:
            og_title = soup.find("meta", property="og:title")
            title = clean_cell(og_title.get("content")) if og_title and og_title.get("content") else ""
        if not title and soup.title:
            title = clean_cell(soup.title.get_text(" ").split("|")[0])
        if not title:
            title_match = re.search(r"^Title:\s*(.+)$", response.text, re.M) or re.search(r"^#\s+(.+)$", response.text, re.M)
            if title_match:
                title = clean_cell(title_match.group(1).split("|")[0].replace(" - Red Sirocco", ""))

        visible_text = self.visible_page_text(soup)
        descriptions = [visible_text]
        price = extract_price_from_text(visible_text)
        if jsonld_product:
            descriptions.insert(0, clean_text(jsonld_product.get("description")))
            price = extract_price_from_offers(jsonld_product.get("offers")) or price
        source_text = "\n".join(part for part in descriptions if part)
        fields = extract_product_fields(source_text, title, [], jsonld_product)
        raw_search = clean_cell(" ".join([title, url]), 500)

        return Product(
            roaster=entry.roaster,
            name=title,
            roast_profile=fields.get("roast_profile", ""),
            flavour_notes=fields.get("flavour_notes", ""),
            origin_location=fields.get("origin_location", ""),
            varietal_species=fields.get("varietal_species", ""),
            process=fields.get("process", ""),
            elevation=fields.get("elevation", ""),
            price=price,
            product_link=product_link,
            raw_search_text=raw_search,
        )

    def visible_page_text(self, soup: BeautifulSoup) -> str:
        clone = BeautifulSoup(str(soup), "html.parser")
        for tag in clone(["script", "style", "noscript", "svg"]):
            tag.decompose()
        lines = [clean_cell(line, 300) for line in clone.get_text("\n").splitlines()]
        return "\n".join(line for line in lines if line)

    def find_jsonld_product(self, soup: BeautifulSoup) -> dict[str, Any] | None:
        for script in soup.find_all("script", type=lambda value: value and "ld+json" in value):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for item in get_nested_values(data):
                item_type = item.get("@type") if isinstance(item, dict) else None
                types = item_type if isinstance(item_type, list) else [item_type]
                if any(str(value).lower() == "product" for value in types if value):
                    return item
        return None


def build_sheets_service() -> Any:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if raw_json:
        info = json.loads(raw_json)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif credentials_file:
        credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
    else:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS")
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def get_values(service: Any, spreadsheet_id: str, sheet_name: str, columns: list[str]) -> list[list[str]]:
    end_col = column_letter(len(columns))
    request = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=sheet_range(sheet_name, f"A:{end_col}"))
    result = execute_google_request(request, f"read {sheet_name}")
    values = result.get("values", [])
    return [row + [""] * (len(columns) - len(row)) for row in values]


def execute_google_request(request: Any, action: str) -> dict[str, Any]:
    for attempt in range(1, GOOGLE_API_MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            retryable = status is None or status in {429, 500, 502, 503, 504}
            if not retryable or attempt == GOOGLE_API_MAX_ATTEMPTS:
                raise
            delay = min(2 ** attempt, 30)
            label = f"HTTP {status}" if status is not None else type(exc).__name__
            print(f"Google Sheets API {action} failed with {label}; retrying in {delay}s ({attempt}/{GOOGLE_API_MAX_ATTEMPTS})", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"Google Sheets API {action} failed")


def append_rows(service: Any, spreadsheet_id: str, sheet_name: str, rows: list[list[str]]) -> None:
    if not rows:
        return
    for start in range(0, len(rows), GOOGLE_WRITE_CHUNK_SIZE):
        chunk = rows[start : start + GOOGLE_WRITE_CHUNK_SIZE]
        request = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=sheet_range(sheet_name, "A1"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": chunk},
        )
        execute_google_request(request, f"append {sheet_name}")


def batch_update_values(service: Any, spreadsheet_id: str, updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    for start in range(0, len(updates), GOOGLE_WRITE_CHUNK_SIZE):
        chunk = updates[start : start + GOOGLE_WRITE_CHUNK_SIZE]
        request = service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": chunk},
        )
        execute_google_request(request, "batch update values")


def validate_header(values: list[list[str]], expected: list[str], sheet_name: str) -> None:
    header = values[0] if values else []
    if header[: len(expected)] != expected:
        raise RuntimeError(f"{sheet_name} header mismatch. Expected {expected}; got {header}")


def load_roster(values: list[list[str]]) -> list[RosterEntry]:
    entries: list[RosterEntry] = []
    for row_number, row in enumerate(values[1:], start=2):
        roaster = clean_cell(row[1])
        source_url = clean_cell(row[2])
        if not roaster:
            continue
        entries.append(
            RosterEntry(
                row_number=row_number,
                enabled=parse_enabled(row[0]),
                roaster=roaster,
                source_url=source_url,
                platform=clean_cell(row[3]),
                exclude_keywords=split_keywords(row[4]),
                example_product_link=clean_cell(row[5]),
                notes=clean_cell(row[8]),
            )
        )
    return entries


def should_update_field(old_value: str, new_value: str) -> bool:
    old_clean = clean_cell(old_value)
    new_clean = clean_cell(new_value)
    if not new_clean:
        return False
    return old_clean != new_clean


def prepare_changes(
    run: str,
    master_values: list[list[str]],
    rosters: list[RosterEntry],
    scraper: CoffeeScraper,
) -> tuple[list[list[str]], list[dict[str, Any]], list[list[str]], list[list[str]], list[dict[str, Any]], dict[str, int]]:
    master_rows = master_values[1:]
    master_by_link: dict[str, tuple[int, list[str]]] = {}
    for index, row in enumerate(master_rows, start=2):
        link = row[9] if len(row) > 9 else ""
        if link:
            master_by_link.setdefault(product_key(link), (index, row))

    new_rows: list[list[str]] = []
    cell_updates: list[dict[str, Any]] = []
    change_log_rows: list[list[str]] = []
    error_rows: list[list[str]] = []
    roster_updates: list[dict[str, Any]] = []
    stats = {
        "roasters_checked": 0,
        "products_scanned": 0,
        "new_products": 0,
        "updated_products": 0,
        "errors": 0,
    }

    pending_new_links: set[str] = set()
    for entry in rosters:
        if not entry.enabled:
            continue
        if not entry.source_url:
            stats["errors"] += 1
            error_rows.append(
                [timestamp(), run, entry.roaster, entry.source_url, "MissingSourceURL", "Source URL is blank", "Add a source URL in Roster Checklist", "FALSE"]
            )
            roster_updates.extend(roster_status_updates(entry.row_number, "Error: source URL missing"))
            continue

        try:
            products = scraper.scrape(entry)
            stats["roasters_checked"] += 1
            stats["products_scanned"] += len(products)
            updated_links_for_roaster: set[str] = set()

            for product in products:
                key = product_key(product.product_link)
                if not key:
                    continue
                if key not in master_by_link and key not in pending_new_links:
                    new_rows.append(product.as_master_row())
                    pending_new_links.add(key)
                    stats["new_products"] += 1
                    change_log_rows.append(
                        [timestamp(), run, product.roaster, "Added", product.name, product.product_link, "", "", "", "New product found from source URL"]
                    )
                    continue

                existing = master_by_link.get(key)
                if not existing:
                    continue
                row_number, existing_row = existing
                product_values = product.as_master_row()
                for col_index, column in enumerate(MASTER_COLUMNS, start=1):
                    if column == "Product Link":
                        continue
                    old = existing_row[col_index - 1] if len(existing_row) >= col_index else ""
                    new = product_values[col_index - 1]
                    if should_update_field(old, new):
                        cell = f"{column_letter(col_index)}{row_number}"
                        cell_updates.append({"range": sheet_range(MASTER_SHEET, cell), "values": [[new]]})
                        existing_row[col_index - 1] = new
                        updated_links_for_roaster.add(key)
                        change_log_rows.append(
                            [timestamp(), run, product.roaster, "Updated", product.name, product.product_link, column, old, new, "Source value changed"]
                        )

            stats["updated_products"] += len(updated_links_for_roaster)
            roster_updates.extend(
                roster_status_updates(entry.row_number, f"OK: {len(products)} scanned, {len(updated_links_for_roaster)} updated, {sum(1 for row in new_rows if row[0] == entry.roaster)} new")
            )
        except Exception as exc:
            stats["errors"] += 1
            message = clean_cell(str(exc), 400)
            print(f"Error scraping {entry.roaster}: {type(exc).__name__}: {message}", file=sys.stderr)
            error_rows.append(
                [timestamp(), run, entry.roaster, entry.source_url, type(exc).__name__, message, "Review scraper/source URL", "FALSE"]
            )
            roster_updates.extend(roster_status_updates(entry.row_number, f"Error: {message[:180]}"))
            traceback.print_exc()

    return new_rows, cell_updates, change_log_rows, error_rows, roster_updates, stats


def roster_status_updates(row_number: int, status_text: str) -> list[dict[str, Any]]:
    return [
        {"range": sheet_range(ROSTER_SHEET, f"G{row_number}"), "values": [[timestamp()]]},
        {"range": sheet_range(ROSTER_SHEET, f"H{row_number}"), "values": [[clean_cell(status_text, 240)]]},
    ]


def run_automation(dry_run: bool = False) -> int:
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID", SHEET_ID_DEFAULT).strip()
    run = run_id()
    started = timestamp()
    service = build_sheets_service()

    master_values = get_values(service, spreadsheet_id, MASTER_SHEET, MASTER_COLUMNS)
    roster_values = get_values(service, spreadsheet_id, ROSTER_SHEET, ROSTER_COLUMNS)
    validate_header(master_values, MASTER_COLUMNS, MASTER_SHEET)
    validate_header(roster_values, ROSTER_COLUMNS, ROSTER_SHEET)

    rosters = load_roster(roster_values)
    scraper = CoffeeScraper()
    new_rows, cell_updates, change_rows, error_rows, roster_updates, stats = prepare_changes(run, master_values, rosters, scraper)
    status = "OK" if stats["errors"] == 0 else "Completed with errors"

    notification_sent = False
    finished = timestamp()
    run_log_row = [
        timestamp(),
        run,
        started,
        finished,
        status,
        str(stats["roasters_checked"]),
        str(stats["products_scanned"]),
        str(stats["new_products"]),
        str(stats["updated_products"]),
        str(stats["errors"]),
        "TRUE" if notification_sent else "FALSE",
        "Dry run; no sheets written; notifications disabled" if dry_run else "Notifications disabled",
    ]

    if dry_run:
        print(json.dumps({"run_id": run, "status": status, "stats": stats, "notification_sent": notification_sent}, indent=2))
        return 0

    append_rows(service, spreadsheet_id, MASTER_SHEET, new_rows)
    batch_update_values(service, spreadsheet_id, cell_updates + roster_updates)
    append_rows(service, spreadsheet_id, CHANGE_LOG_SHEET, change_rows)
    append_rows(service, spreadsheet_id, ERRORS_SHEET, error_rows)
    append_rows(service, spreadsheet_id, RUN_LOG_SHEET, [run_log_row])

    print(json.dumps({"run_id": run, "status": status, "stats": stats, "notification_sent": notification_sent}, indent=2))
    return 0


def check_config() -> int:
    has_google_credentials = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    if not has_google_credentials:
        print("Missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS", file=sys.stderr)
        return 1
    print("Configuration check completed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the Coffee Diary Google Sheet from roaster source URLs.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and compare, but do not write to Google Sheets.")
    parser.add_argument("--check-config", action="store_true", help="Check required environment variables and exit.")
    args = parser.parse_args()
    if args.check_config:
        return check_config()
    return run_automation(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
