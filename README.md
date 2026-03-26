# Price Tracker

Simple GitHub Actions price tracker for HTML pages.

## Files

- `tracker.py`: fetches product pages, extracts prices, and appends to `history.json`
- `products.json`: list of products to track
- `history.json`: append-only history per product
- `.github/workflows/price-tracker.yml`: scheduled runner and auto-commit

## Product format

Each product entry supports:

- `name`: display name
- `url`: page to fetch
- `selector`: CSS selector for the price element
- `currency`: optional currency label
- `attribute`: optional HTML attribute to read instead of text
- `regex`: optional regex to isolate the number from the extracted text
- `html_regex`: optional regex to extract the value directly from the raw HTML instead of a DOM node
- `headers`: optional per-product HTTP headers for sites that block bot-like requests
- `enabled`: disable entries without deleting them

Example:

```json
{
  "name": "Widget",
  "url": "https://shop.example.com/widget",
  "selector": "[data-test='price']",
  "currency": "USD",
  "regex": "(\\d+[\\d,.]*(?:\\.\\d{2})?)",
  "headers": {
    "Referer": "https://shop.example.com/"
  }
}
```

For sites that embed the price in JSON instead of a stable DOM element:

```json
{
  "name": "Widget",
  "url": "https://shop.example.com/widget",
  "currency": "USD",
  "html_regex": "\"price\":\"([0-9]+(?:\\.[0-9]+)?)\""
}
```

## Local usage

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python tracker.py --dry-run
python tracker.py
```

## Notes

- The workflow runs daily and can also be triggered manually.
- `STRICT_FAILURES=true` makes the action fail if any product fetch fails.
- Price drops are reported into one rolling GitHub issue labeled `price-alert`.
- Price drops can also be sent to WhatsApp through Twilio when the relevant GitHub secrets are configured.
- The issue is created on the first drop and receives a new comment for each later drop event.
- GitHub Actions creates the `price-alert` label automatically if it does not already exist.
- Some stores return `403 Forbidden` to obvious bots. The tracker now uses browser-like defaults and can set per-product headers when needed.
- For Cloudflare-protected stores, the tracker prefers `cloudscraper` and falls back to plain `requests` if it is unavailable.
- Keep selectors specific. Most tracker failures come from fragile selectors.

## Price-drop notifications

When a product price decreases, `tracker.py` writes `price-drop.md`. The GitHub Actions workflow then:

- creates an issue titled `Rolling Price Alerts` with label `price-alert` if one does not exist
- otherwise adds the new drop report as a comment on the existing open issue

This uses the built-in `GITHUB_TOKEN`, so there is no extra secret to manage.

For WhatsApp alerts, `tracker.py` also writes `price-drop-whatsapp.txt`. If all of these GitHub Actions secrets are present, the workflow sends that message through Twilio's WhatsApp API:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_FROM` (for example `whatsapp:+14155238886`)
- `TWILIO_WHATSAPP_TO` (for example your verified destination number like `whatsapp:+6421...`)

If any of those secrets are missing, the WhatsApp step is skipped and the GitHub issue flow still runs as usual.

For a manual end-to-end test, run the workflow from GitHub Actions using the `Send a WhatsApp test message` input. Manual runs bypass the 8 AM New Zealand time check and create a temporary `price-drop-whatsapp.txt` test message so the Twilio step can send without requiring a real price drop.
