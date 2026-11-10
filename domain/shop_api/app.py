from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from .data import ADS, CURRENCIES, PRODUCTS, PRODUCTS_BY_ID
from .telemetry import configure_telemetry

app = FastAPI(title="AI Lab Shop API", version="0.1.0")
configure_telemetry(app)

CART: dict[str, dict[str, int]] = {}
ORDERS: list[dict[str, Any]] = []


class CartItem(BaseModel):
    productId: str
    quantity: int = 1


class AddToCartRequest(BaseModel):
    item: CartItem
    userId: str


class EmptyCartRequest(BaseModel):
    userId: str


class Address(BaseModel):
    streetAddress: str
    city: str
    state: str
    country: str
    zipCode: str


class CreditCardInfo(BaseModel):
    creditCardNumber: str
    creditCardCvv: int
    creditCardExpirationYear: int
    creditCardExpirationMonth: int


class CheckoutRequest(BaseModel):
    userId: str
    userCurrency: str = "USD"
    address: Address
    email: str
    creditCard: CreditCardInfo


def _cart_payload(user_id: str, enrich: bool = False) -> dict:
    items = []
    for product_id, quantity in CART.get(user_id, {}).items():
        item: dict[str, Any] = {"productId": product_id, "quantity": quantity}
        if enrich:
            item["product"] = PRODUCTS_BY_ID.get(product_id)
        items.append(item)
    return {"userId": user_id, "items": items}


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "shop-api"}


@app.get("/api/products")
def list_products() -> list[dict]:
    return PRODUCTS


@app.get("/api/products/{product_id}")
def get_product(product_id: str) -> dict:
    product = PRODUCTS_BY_ID.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


@app.get("/api/currency")
def get_currencies() -> list[str]:
    return CURRENCIES


@app.get("/api/data")
def get_ads(contextKeys: str | None = Query(default=None)) -> list[dict]:
    keys = [key for key in (contextKeys or "").split(",") if key]
    ads: list[dict] = []
    for key in keys:
        ads.extend(ADS.get(key, []))
    return ads or ADS["default"]


@app.get("/api/recommendations")
def get_recommendations(productIds: str | None = Query(default=None)) -> list[dict]:
    requested = {
        product_id for product_id in (productIds or "").split(",") if product_id
    }
    candidates = [
        product for product in PRODUCTS if product["id"] not in requested
    ]
    return candidates[:4]


@app.get("/api/cart")
def get_cart(user_id: str = Query(default="")) -> dict:
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    return _cart_payload(user_id, enrich=True)


@app.post("/api/cart")
def add_to_cart(payload: AddToCartRequest) -> dict:
    if payload.item.productId not in PRODUCTS_BY_ID:
        raise HTTPException(status_code=404, detail="product not found")
    if payload.item.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be positive")

    cart = CART.setdefault(payload.userId, {})
    cart[payload.item.productId] = (
        cart.get(payload.item.productId, 0) + payload.item.quantity
    )
    return _cart_payload(payload.userId)


@app.delete("/api/cart")
def empty_cart(payload: EmptyCartRequest) -> Response:
    CART.pop(payload.userId, None)
    return Response(status_code=204)


@app.get("/api/shipping")
def get_shipping_quote(
    itemList: str = Query(...),
    currencyCode: str = Query(default="USD"),
    address: str = Query(...),
) -> dict:
    try:
        items = json.loads(itemList)
        json.loads(address)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    total_quantity = sum(int(item.get("quantity", 1)) for item in items)
    units = 5 + 2 * total_quantity
    return {"currencyCode": currencyCode, "units": units, "nanos": 0}


@app.post("/api/checkout")
def checkout(payload: CheckoutRequest) -> dict:
    cart = CART.get(payload.userId, {})
    if not cart:
        raise HTTPException(status_code=400, detail="cart is empty")

    items = []
    total_units = 0
    for product_id, quantity in cart.items():
        product = PRODUCTS_BY_ID.get(product_id)
        if product is None:
            continue
        item_units = int(product["priceUsd"]["units"]) * quantity
        total_units += item_units
        items.append(
            {
                "item": {
                    "productId": product_id,
                    "quantity": quantity,
                    "product": product,
                },
                "cost": {
                    "currencyCode": "USD",
                    "units": item_units,
                    "nanos": 0,
                },
            }
        )

    order = {
        "orderId": str(uuid.uuid4()),
        "shippingTrackingId": str(uuid.uuid4()),
        "shippingCost": {"currencyCode": "USD", "units": 5, "nanos": 0},
        "shippingAddress": payload.address.model_dump(),
        "items": items,
        "userId": payload.userId,
        "userCurrency": payload.userCurrency,
        "total": {"currencyCode": "USD", "units": total_units, "nanos": 0},
        "createdAt": datetime.now(UTC).isoformat(),
    }
    ORDERS.append(order)
    CART.pop(payload.userId, None)
    return order
