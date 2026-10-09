
import os
import json
import re
import requests
from pathlib import Path
from bs4 import BeautifulSoup

MAX_PRICE_DKK = 8000
STATE_FILE = Path("state.json")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Norlys er helt fjernet.
STORES = [
    {
        "name": "Elgiganten",
        "url": "https://www.elgiganten.dk/product/gaming/spillekonsol-tilbehor/playstation/ps5-playstation-5-spillekonsol/playstation-5-pro-2tb-spillekonsol/994805",
    },
    {
        "name": "POWER",
        "url": "https://www.power.dk/gaming/playstation/playstation-konsoll/playstation-5-pro-konsoll/p-3645713/",
    },
    {
        "name": "Proshop",
        "url": "https://www.proshop.dk/Sony-PlayStation-5-Pro",
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


def telegram(message):
    response = session.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        print("ADVARSEL: state.json kunne ikke læses. Starter med tom status.")
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_json(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_json(value)


def parse_price(raw_price):
    if raw_price is None:
        return None

    value = str(raw_price).strip()
    value = re.sub(r"[^0-9,.]", "", value)

    if not value:
        return None

    # Eksempel: 7.499,00 bliver til 7499.00.
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")

    try:
        return float(value)
    except ValueError:
        return None


def is_ps5_pro_product(item):
    name = item.get("name", "")
    if not isinstance(name, str):
        return False

    normalized = re.sub(r"[^a-z0-9]+", " ", name.lower())
    has_console_name = (
        "playstation 5" in normalized
        or re.search(r"\bps5\b", normalized) is not None
    )
    has_pro = re.search(r"\bpro\b", normalized) is not None

    return has_console_name and has_pro


def product_data(soup):
    """
    Finder pris og lagerstatus for PS5 Pro-produktet.

    En pris eller teksten 'læg i kurv' er ikke bevis på lager.
    Positiv lagerstatus kræver eksplicit strukturerede produktdata.
    """
    matching_products = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        for item in walk_json(data):
            item_types = item.get("@type", [])
            if isinstance(item_types, str):
                item_types = [item_types]

            if not any(
                product_type in ("Product", "ProductGroup")
                for product_type in item_types
            ):
                continue

            if is_ps5_pro_product(item):
                matching_products.append(item)

    prices = []
    availabilities = []

    for product in matching_products:
        offers = product.get("offers", [])

        if isinstance(offers, dict):
            offers = [offers]

        for offer in offers:
            if not isinstance(offer, dict):
                continue

            currency = offer.get("priceCurrency")
            raw_price = offer.get("price")

            if raw_price is None:
                spec = offer.get("priceSpecification", {})
                if isinstance(spec, dict):
                    raw_price = spec.get("price")
                    currency = spec.get("priceCurrency", currency)

            if currency == "DKK":
                price = parse_price(raw_price)
                if price is not None and price > 0:
                    prices.append(price)

            availability = offer.get("availability")
            if availability:
                availabilities.append(str(availability).lower())

    price = min(prices) if prices else None

    if not availabilities:
        return price, "UNKNOWN"

    # Ved modstridende data vælger vi den forsigtige status.
    out_markers = (
        "outofstock",
        "soldout",
        "discontinued",
    )
    in_markers = (
        "instock",
        "limitedavailability",
    )

    if any(
        any(marker in value for marker in out_markers)
        for value in availabilities
    ):
        return price, "OUT"

    if all(
        any(marker in value for marker in in_markers)
        for value in availabilities
    ):
        return price, "IN"

    return price, "UNKNOWN"


def check_store(store):
    try:
        response = session.get(store["url"], timeout=25)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type:
            print(f"{store['name']}: UNKNOWN - svaret er ikke HTML.")
            return "UNKNOWN", None

        soup = BeautifulSoup(response.text, "html.parser")
        price, status = product_data(soup)

        # Ingen fallback baseret på pris, "læg i kurv" eller
        # generelle lagerord på siden.
        # Ukendte data skal aldrig skabe en lageralarm.
        return status, price

    except requests.RequestException as error:
        print(f"FEJL: {store['name']}: {error}")
        return "ERROR", None


def previous_status(entry):
    # Understøtter både den gamle og den nye state.json-struktur.
    if isinstance(entry, dict):
        return entry.get("status")
    return entry


def main():
    state = load_state()

    for store in STORES:
        name = store["name"]
        status, price = check_store(store)
        old_status = previous_status(state.get(name))

        price_ok = (
            price is not None
            and 0 < price <= MAX_PRICE_DKK
        )

        print(
            f"{name}: status={status}, pris={price}, "
            f"tidligere_status={old_status}"
        )

        if status == "OUT":
            state[name] = {
                "status": "OUT",
                "price": price,
            }
            continue

        if status == "IN" and price_ok:
            if old_status != "IN":
                telegram(
                    "PS5 PRO: PRODUKTDATA ANGIVER LAGER\n"
                    f"Butik: {name}\n"
                    f"Pris: {price:.2f} kr.\n"
                    "Kontrollér, at varen faktisk kan bestilles:\n"
                    f"{store['url']}"
                )

            state[name] = {
                "status": "IN",
                "price": price,
            }
            continue

        # UNKNOWN/ERROR overskriver ikke den sidste kendte status.
        print(f"{name}: Ingen sikker lageralarm.")

    # Fjern eventuelle gamle Norlys-data fra state.json.
    state.pop("Norlys", None)

    save_state(state)


if __name__ == "__main__":
    main()
