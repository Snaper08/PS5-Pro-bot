
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
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
    )
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
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
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

    # Håndter danske priser som 7.499,00.
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


def product_data(soup):
    """Returnér pris og eksplicit struktureret lagerstatus."""
    prices = []
    availabilities = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        for item in walk_json(data):
            types = item.get("@type", [])
            if isinstance(types, str):
                types = [types]

            if "Product" not in types:
                continue

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
                    if isinstance(spec, dict):
                        raw_price = spec.get("price")
                        currency = spec.get("priceCurrency", currency)

                parsed = parse_price(raw_price)
                if parsed is not None and currency == "DKK":
                    prices.append(parsed)

                availability = offer.get("availability")
                if availability:
                    availability = str(availability).lower()
                    availabilities.append(availability)

    # Meta-tags kan give en pris, men ikke i sig selv bevise lager.
    for selector in [
        'meta[property="product:price:amount"]',
        'meta[itemprop="price"]',
    ]:
        tag = soup.select_one(selector)
        if not tag:
            continue

        currency_tag = soup.select_one(
            'meta[property="product:price:currency"]'
        )
        currency = currency_tag.get("content") if currency_tag else None

        if currency == "DKK" or tag.get("itemprop") == "price":
            parsed = parse_price(tag.get("content"))
            if parsed is not None:
                prices.append(parsed)
                break

    # Brug den laveste fundne pris, hvis flere prisfelter findes.
    price = min(prices) if prices else None

    # Struktureret lagerstatus er eneste positive lagerbevis.
    # Hvis tilbuddene er modstridende, vælger vi den forsigtige status.
    availability = None

    if availabilities:
        if any(
            "outofstock" in value
            or "soldout" in value
            or "discontinued" in value
            for value in availabilities
        ):
            availability = "OUT"
        elif all(
            "instock" in value
            or "limitedavailability" in value
            for value in availabilities
        ):
            availability = "IN"
        else:
            availability = "UNKNOWN"

    return price, availability


def check_store(store):
    try:
        response = session.get(store["url"], timeout=25)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        price, availability = product_data(soup)
         if store["name"] == "Norlys":
         print("Norlys: Lagerstatus kræver særskilt kontrol.")
         return "UNKNOWN", price
        # Tydelige tegn på manglende tilgængelighed har forrang.
        # Fjern scripts/styles, så skjult kode ikke tæller som tekst.
        for element in soup(["script", "style", "noscript"]):
            element.decompose()

        text = soup.get_text(" ", strip=True).lower()
        text = re.sub(r"\s+", " ", text)

        unavailable_phrases = [
            "udsolgt",
            "ikke på lager",
            "ikke tilgængelig",
            "ikke tilgaengelig",
            "midlertidigt udsolgt",
            "kan ikke bestilles",
            "not available",
            "out of stock",
            "sold out",
        ]

        if any(phrase in text for phrase in unavailable_phrases):
            return "OUT", price

        # IN kræver eksplicit struktureret lagerstatus.
        # "Læg i kurv", en pris eller en almindelig produktside er
        # IKKE tilstrækkeligt bevis.
        if availability == "IN":
            return "IN", price

        if availability == "OUT":
            return "OUT", price

        return "UNKNOWN", price

    except requests.RequestException as error:
        print(f"FEJL: {store['name']}: {error}")
        return "ERROR", None


def main():
    state = load_state()

    for store in STORES:
        name = store["name"]
        status, price = check_store(store)

        # Understøtter både gamle state.json-værdier og nye.
        old_entry = state.get(name, {})
        if isinstance(old_entry, dict):
            old_status = old_entry.get("status")
        else:
            old_status = old_entry

        price_ok = price is not None and 0 < price <= MAX_PRICE_DKK

        print(
            f"{name}: status={status}, pris={price}, "
            f"tidligere_status={old_status}"
        )

        if status == "IN" and price_ok:
            if old_status != "IN":
                telegram(
                    f"PS5 PRO: LAGERSTATUS ANGIVET SOM TILGÆNGELIG\n"
                    f"Butik: {name}\n"
                    f"Pris: {price:.2f} kr.\n"
                    f"Kontrollér, at varen kan bestilles:\n{store['url']}"
                )

            state[name] = {
                "status": "IN",
                "price": price,
            }

        elif status == "OUT":
            state[name] = {
                "status": "OUT",
                "price": price,
            }

        else:
            # UNKNOWN/ERROR overskriver ikke seneste kendte status.
            # Det forhindrer usikre fund i at blive gemt som IN.
            print(f"{name}: Ingen sikker lagerstatus; ingen alarm.")

    save_state(state)


if __name__ == "__main__":
    main()
