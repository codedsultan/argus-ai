from __future__ import annotations

import json
from pathlib import Path

_DATA_DIR = Path(__file__).parent

# The catalog mirrors the tool result recorded in the replay fixtures so the
# replayed final answers stay coherent with the local shop.
CURRENCIES = [
    "ZAR",
    "ISK",
    "ILS",
    "RON",
    "GBP",
    "BRL",
    "CZK",
    "AUD",
    "BGN",
    "JPY",
    "PHP",
    "HUF",
    "USD",
    "SGD",
    "IDR",
    "CHF",
    "TRY",
    "NZD",
    "THB",
    "DKK",
    "PLN",
    "NOK",
    "HRK",
    "HKD",
    "EUR",
    "RUB",
    "CAD",
    "INR",
    "SEK",
    "KRW",
    "CNY",
    "MXN",
    "MYR",
]

PRODUCTS = json.loads((_DATA_DIR / "products.json").read_text(encoding="utf-8"))
PRODUCTS_BY_ID = {product["id"]: product for product in PRODUCTS}

ADS = {
    "telescopes": [
        {
            "redirectUrl": "/product/66VCHSJNUP",
            "text": (
                "Meet the Starfinder 70, a beginner-friendly refractor with a "
                "smartphone mount."
            ),
        }
    ],
    "accessories": [
        {
            "redirectUrl": "/product/L9ECAV7KIM",
            "text": "Keep your optics clean with the Optics Care Kit.",
        }
    ],
    "travel": [
        {
            "redirectUrl": "/product/1YMWWN1N4O",
            "text": "Take the night sky with you using the Horizon Travel Scope.",
        }
    ],
    "binoculars": [
        {
            "redirectUrl": "/product/2ZYFJ3GM2N",
            "text": "Ridgeview 8x42 binoculars are 50% off this week.",
        }
    ],
    "default": [
        {
            "redirectUrl": "/product/HQTGWGPNH4",
            "text": "Read The Night Watcher's Almanac for seasonal sky charts.",
        }
    ],
}
