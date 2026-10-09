
import os
import json
import re
import requests
from bs4 import BeautifulSoup
from pathlib import Path

MAX_PRICE_DKK = 8000
STATE_FILE = Path("state.json")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Start med få butikker. Tilføj flere efter test.
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
    {
        "name": "Norlys",
        "url": "https://shop.norlys.dk/shop/accessories/sony-playstation-5-pro-4b3d628f/",
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalStockMonitor/1.0)"
}

session = requests.Session()
session.headers.update(HEADERS)


def telegram(message):
    response = session.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def walk_json(obj):
    """Gå rekursivt gennem JSON-LD-data."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_json(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_json(value)


def product_data(soup):
    """Find struktureret pris og lagerstatus, hvis siden tilbyder dem."""
    price = None
    availability = None

    # Først: produktets JSON-LD-data.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        for item in walk_json(data):
            if item.get("@type") == "Product":
                offers = item.get("offers", [])
                if isinstance(offers, dict):
                    offers = [offers]

                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    currency = offer.get("priceCurrency")
                    raw_price = offer.get("price")

                    if raw_price is None:
                        spec = offer.get("priceSpecification", {})
                        raw_price = spec.get("price")
                        currency = spec.get("priceCurrency", currency)

                    if raw_price is not None and currency == "DKK":
                        try:
                            price = float(str(raw_price).replace(",", "."))
                        except ValueError:
                            pass

                    if offer.get("availability"):
                        availability = str(offer["availability"]).lower()

                    if price is not None and availability:
                        return price, availability

    # Alternativ: almindelige pris-meta-tags.
    for selector in [
        'meta[property="product:price:amount"]',
        'meta[itemprop="price"]',
    ]:
        tag = soup.select_one(selector)
        if tag:
            currency_tag = soup.select_one(
                'meta[property="product:price:currency"]'
            )
            currency = currency_tag.get("content") if currency_tag else None

            if currency == "DKK" or tag.get("itemprop") == "price":
                try:
                    price = float(tag.get("content", "").replace(",", "."))
                except ValueError:
                    pass
                if price is not None:
                    break

    # Meta-tags fortæller ikke nødvendigvis lagerstatus.
    for tag in soup.select('link[itemprop="availability"]'):
        availability = str(tag.get("href", "")).lower()
        if availability:
            break

    return price, availability


def check_store(store):
    try:
        response = session.get(store["url"], timeout=25)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        price, availability = product_data(soup)

        # Struktureret lagerstatus er bedst, når den findes.
        if availability:
            if "outofstock" in availability or "soldout" in availability:
                return "OUT", price
            if "instock" in availability or "limitedavailability" in availability:
                status = "IN"
            else:
                status = "UNKNOWN"
        else:
            # Forsigtig fallback. Webshoppen kan kræve en specialtilpasning.
            text = soup.get_text(" ", strip=True).lower()

            if any(word in text for word in [
                "udsolgt", "ikke på lager", "midlertidigt udsolgt"
            ]):
                status = "OUT"
            elif any(word in text for word in [
                "på lager", "læg i kurv", "tilføj til kurv"
            ]):
                status = "POSSIBLE"
            else:
                status = "UNKNOWN"

        return status, price

    except requests.RequestException as error:
        print(f"FEJL: {store['name']}: {error}")
        return "ERROR", None


def main():
    state = load_state()

    for store in STORES:
        name = store["name"]
        status, price = check_store(store)
        old_status = state.get(name)

        print(f"{name}: status={status}, pris={price}")

        # Kræv en kendt pris under grænsen.
        price_ok = price is not None and 0 < price <= MAX_PRICE_DKK

        # En fallback-status giver kun en forsigtig advarsel.
        if status in ("IN", "POSSIBLE") and price_ok:
            if old_status != "IN":
                telegram(
                    f"PS5 PRO: MULIGT LAGERFUND\n"
                    f"Butik: {name}\n"
                    f"Pris: {price:.2f} kr.\n"
                    f"Status: {status}\n"
                    f"Tjek straks, om den kan bestilles:\n{store['url']}"
                )
            state[name] = "IN"

        elif status == "OUT":
            state[name] = "OUT"

        # UNKNOWN/ERROR overskriver ikke den sidste kendte status.

    save_state(state)


if __name__ == "__main__":
    main()
