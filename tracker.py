#!/usr/bin/env python3
import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_PATH = BASE_DIR / "products.json"
HISTORY_PATH = BASE_DIR / "history.json"
ALERT_PATH = BASE_DIR / "price-drop.md"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "price-tracker-bot/1.0 (+https://github.com/)"
MAX_HISTORY_PER_PRODUCT = 90


@dataclass
class Product:
    name: str
    url: str
    selector: str
    currency: str = ""
    attribute: str | None = None
    regex: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def parse_products(raw_products: Any) -> list[Product]:
    if not isinstance(raw_products, list):
        raise ValueError("products.json must contain a JSON array")

    parsed: list[Product] = []
    for index, item in enumerate(raw_products, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Product #{index} must be a JSON object")
        try:
            product = Product(
                name=str(item["name"]).strip(),
                url=str(item["url"]).strip(),
                selector=str(item["selector"]).strip(),
                currency=str(item.get("currency", "")).strip(),
                attribute=(
                    str(item["attribute"]).strip() if item.get("attribute") else None
                ),
                regex=str(item["regex"]).strip() if item.get("regex") else None,
                headers=(
                    {
                        str(key): str(value)
                        for key, value in item.get("headers", {}).items()
                    }
                    if item.get("headers")
                    else None
                ),
                enabled=bool(item.get("enabled", True)),
            )
        except KeyError as exc:
            raise ValueError(f"Product #{index} is missing required field {exc}") from exc

        if not product.name or not product.url or not product.selector:
            raise ValueError(
                f"Product #{index} must define non-empty name, url, and selector"
            )
        parsed.append(product)

    return parsed


def build_session() -> Any:
    try:
        import cloudscraper

        session = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
            }
        )
    except ImportError:
        import requests

        session = requests.Session()

    session.headers.update(
        {
            "User-Agent": os.getenv(
                "PRICE_TRACKER_USER_AGENT",
                (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/134.0.0.0 Safari/537.36"
                ),
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-NZ,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return session


def extract_price_text(product: Product, html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(product.selector)
    if node is None:
        raise ValueError(f"CSS selector did not match any node: {product.selector}")

    if product.attribute:
        value = node.get(product.attribute)
        if value is None:
            raise ValueError(
                f"Attribute '{product.attribute}' was not present on matched element"
            )
        return str(value).strip()

    return node.get_text(" ", strip=True)


def normalize_price(price_text: str, regex: str | None = None) -> Decimal:
    source_text = price_text
    if regex:
        match = re.search(regex, source_text)
        if not match:
            raise ValueError(f"Regex did not match extracted text: {regex}")
        source_text = match.group(1) if match.groups() else match.group(0)

    normalized = source_text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        raise ValueError(f"Could not parse a numeric price from: {price_text}")

    try:
        return Decimal(match.group(0)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Could not normalize price: {price_text}") from exc


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def fetch_product(session: Any, product: Product) -> dict[str, Any]:
    headers = dict(product.headers or {})
    if "Referer" not in headers:
        headers["Referer"] = "https://www.chemistwarehouse.co.nz/"

    response = session.get(
        product.url,
        headers=headers,
        timeout=int(os.getenv("PRICE_TRACKER_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
    )
    response.raise_for_status()

    price_text = extract_price_text(product, response.text)
    price_value = normalize_price(price_text, product.regex)
    return {
        "status": "ok",
        "checked_at": utc_now(),
        "url": product.url,
        "currency": product.currency,
        "price": f"{price_value:.2f}",
        "raw_text": price_text,
    }


def get_last_success(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in reversed(entries):
        if entry.get("status") == "ok":
            return entry
    return None


def append_history(
    history: dict[str, Any], product_name: str, result: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    products_history = history.setdefault("products", {})
    entries = products_history.setdefault(product_name, [])
    previous = get_last_success(entries)
    entries.append(result)
    del entries[:-MAX_HISTORY_PER_PRODUCT]

    if result.get("status") != "ok":
        return previous, "error"
    if previous is None:
        return previous, "new"
    if previous.get("price") != result.get("price"):
        return previous, "changed"
    return previous, "unchanged"


def write_summary(lines: list[str]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def write_alert_file(drops: list[dict[str, Any]]) -> None:
    if not drops:
        ALERT_PATH.unlink(missing_ok=True)
        return

    lines = ["## Price drops detected", "", f"Checked at `{utc_now()}`", ""]
    for drop in drops:
        lines.append(
            f"- **{drop['name']}**: {drop['previous_price']} -> {drop['current_price']} {drop['currency']}".rstrip()
        )
        lines.append(f"  {drop['url']}")
    ALERT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_tracker(dry_run: bool) -> int:
    products = parse_products(load_json(PRODUCTS_PATH, []))
    history = load_json(HISTORY_PATH, {"products": {}})

    enabled_products = [product for product in products if product.enabled]
    if dry_run:
        print(f"Validated {len(enabled_products)} enabled products.")
        return 0

    session = build_session()
    summary_lines = [
        "## Price Tracker Run",
        "",
        f"Checked at `{utc_now()}`",
        "",
        "| Product | Status | Price | Change |",
        "| --- | --- | --- | --- |",
    ]

    failures = 0
    drops: list[dict[str, Any]] = []
    for product in enabled_products:
        try:
            result = fetch_product(session, product)
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "error",
                "checked_at": utc_now(),
                "url": product.url,
                "error": str(exc),
            }
            failures += 1

        previous, state = append_history(history, product.name, result)
        if result["status"] == "ok":
            previous_price = previous.get("price") if previous else None
            current_price_decimal = parse_decimal(result["price"])
            previous_price_decimal = parse_decimal(previous_price)
            drop_detected = (
                state == "changed"
                and previous_price_decimal is not None
                and current_price_decimal is not None
                and current_price_decimal < previous_price_decimal
            )
            change_label = (
                "first capture"
                if state == "new"
                else f"{previous_price} -> {result['price']} (drop)"
                if drop_detected
                else f"{previous_price} -> {result['price']}"
                if state == "changed"
                else "no change"
            )
            print(
                f"{product.name}: {result['price']} {product.currency}".strip()
                + f" ({change_label})"
            )
            summary_lines.append(
                f"| {product.name} | ok | {result['price']} {product.currency} | {change_label} |"
            )
            if drop_detected:
                drops.append(
                    {
                        "name": product.name,
                        "url": product.url,
                        "currency": product.currency,
                        "previous_price": previous_price,
                        "current_price": result["price"],
                    }
                )
        else:
            print(f"{product.name}: ERROR - {result['error']}")
            summary_lines.append(
                f"| {product.name} | error | n/a | {result['error']} |"
            )

    if drops:
        summary_lines.extend(
            [
                "",
                "### Price Drops",
                "",
                *[
                    f"- `{drop['name']}`: {drop['previous_price']} -> {drop['current_price']} {drop['currency']} ({drop['url']})"
                    for drop in drops
                ],
            ]
        )

    write_alert_file(drops)
    save_json(HISTORY_PATH, history)
    write_summary(summary_lines)

    strict_failures = os.getenv("STRICT_FAILURES", "").lower() in {"1", "true", "yes"}
    return 1 if strict_failures and failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Track product prices over time.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config files without fetching remote pages.",
    )
    args = parser.parse_args()
    return run_tracker(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
