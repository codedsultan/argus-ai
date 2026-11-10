# Module 0 — Domain and request lifecycle

> Code references are to the scaffold commit `5f62a34`. Line numbers may drift; function and
> file names are the stable anchors.

## What you will learn

- What the fake shop API provides and why its response shapes look the way they
  do.
- How each Python tool maps to one shop endpoint.
- Why tools return error **strings** instead of raising exceptions.
- The full path from `POST /prompt` to the final answer.
- Where the first trace spans come from.

## The loop

```
curl /prompt
   │
   ▼
agent (FastAPI + LangGraph)
   │  model decides to call a tool
   ▼
domain/tools.py  ──HTTP──►  shop-api
   │                         (products, cart, checkout)
   │  tool result
   ▼
model writes the final answer
```

The agent never talks to a database. Its entire world is the ten HTTP endpoints
behind `domain/tools.py`.

## The fake domain

Three files matter:

| File | Purpose |
|---|---|
| `domain/shop_api/app.py` | FastAPI routes, cart and order state |
| `domain/shop_api/data.py` | Currencies, ads, loads the product catalog |
| `domain/shop_api/products.json` | Ten-product Argus catalog fixture |

The API is deliberately small:

| Method | Path | Returns |
|---|---|---|
| `GET` | `/healthz` | `{"status": "ok", "service": "shop-api"}` |
| `GET` | `/api/products` | Full product list |
| `GET` | `/api/products/{id}` | One product, `404` if unknown |
| `GET` | `/api/currency` | Bare list of ISO codes |
| `GET` | `/api/data?contextKeys=...` | Ads for one or more categories |
| `GET` | `/api/recommendations?productIds=...` | Four full product objects |
| `GET` | `/api/cart?user_id=...` | Cart with enriched product objects |
| `POST` | `/api/cart` | Add an item; returns the unenriched cart |
| `DELETE` | `/api/cart` | Empty the cart; `204 No Content` |
| `GET` | `/api/shipping?itemList=...&currencyCode=...&address=...` | `Money` object |
| `POST` | `/api/checkout` | Flat order object |

### Why the shapes look like this

These are not arbitrary. They match the contract the frontend and replay
fixtures consume, which keeps the replayed answers coherent with the local shop:

- **Currency is a bare list.** The frontend expects `["USD", ...]`, not
  `{"currencies": [...]}`.
- **Products are full objects.** Each has `id`, `name`, `description`,
  `picture`, `priceUsd` (`{currencyCode, units, nanos}`), and `categories`.
- **Recommendations are full products.** The model can quote names and prices
  without a second lookup.
- **Cart `GET` enriches items** with a `product` field; cart `POST` does not.
  The frontend uses the enriched form to render the cart.
- **`DELETE /api/cart` returns `204`.** There is no JSON body to parse.
- **Shipping and prices use `Money`**: `{currencyCode, units, nanos}`.
- **Checkout returns a flat order** with `orderId`, `shippingTrackingId`,
  `shippingCost`, `shippingAddress`, `items` (`{item, cost}`), `total`,
  `userCurrency`, and `createdAt`.

Keeping the contract identical means the fake shop can stand in for a real
backend without changing the tools or the agent.

## The tool contract

`domain/tools.py` contains ten async functions. Each one is a thin HTTP client:

| Tool | Endpoint | Side effect |
|---|---|---|
| `list_products` | `GET /api/products` | none |
| `get_product` | `GET /api/products/{id}` | none |
| `get_supported_currencies` | `GET /api/currency` | none |
| `get_ads` | `GET /api/data` | none |
| `get_recommendations` | `GET /api/recommendations` | none |
| `get_cart` | `GET /api/cart` | none |
| `add_to_cart` | `POST /api/cart` | mutates the cart |
| `empty_cart` | `DELETE /api/cart` | mutates the cart |
| `get_shipping_quote` | `GET /api/shipping` | none |
| `checkout` | `POST /api/checkout` | creates an order, empties the cart |

The base URL comes from `APPLICATION_ENDPOINT` (default `localhost:8080`).
Inside compose it is `shop-api:8080`.

## Error strings, not exceptions

Every tool catches its own exceptions and returns a string:

```python
async def list_products():
    """List all products available in the Argus Field Supply."""
    url = f"http://{BASE_URL}/api/products"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.get(url)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        return f"Error while fetching product list: {e}"
```

Why not raise? A raised exception escapes the tool node and aborts the agent run.
A string becomes part of the conversation, so the model can see the failure and
try a different action. This is a deliberate design choice.

Two tools add context to the error:

- `checkout` checks `res.is_success` and returns a message that tells the model
  the cart must contain an item before checkout.
- `get_shipping_quote` validates the item list before making the request, so a
  malformed list becomes a short error string instead of a `422`.

The trade-off: error strings are still model input. Keep them short, stable, and
free of secrets.

## Request lifecycle

```
POST /prompt {message, history}
  │
  ├─ Agent.handle_prompt              services/agent/main.py:43
  │    └─ Agent.run_agent             services/agent/main.py:47  (@workflow)
  │         ├─ ChatLLM()              services/agent/llm.py
  │         ├─ create_agent(...)      LangGraph ReAct graph
  │         └─ agent.ainvoke(...)
  │              ├─ model node: "call list_products"
  │              ├─ tool node: domain.tools.list_products
  │              │    └─ httpx GET shop-api /api/products
  │              ├─ tool result returns to the model
  │              └─ model node: final answer
  │
  └─ {"response": {"messages": [...]}}
```

The `@workflow(name="agent_workflow")` decorator creates the
`agent_workflow.workflow` span. FastAPI instrumentation creates the
`POST /prompt` span. The tool call becomes `execute_tool list_products`; the
model call becomes `ChatLLM.chat`. Guide 2 walks the full trace.

## Run it

Start the stack:

```sh
make core-up-d
```

Check the shop directly:

```sh
curl -s localhost:8080/api/products | jq '.[0]'
curl -s localhost:8080/api/currency | jq '.[0:5]'
curl -s 'localhost:8080/api/recommendations?productIds=0PUK6V6EV0' | jq '.[].name'
curl -s 'localhost:8080/api/data?contextKeys=binoculars' | jq
```

Exercise the cart and checkout with a small script:

```sh
.venv/bin/python - <<'PY'
import httpx

c = httpx.Client(base_url="http://localhost:8080", timeout=10)
product = "0PUK6V6EV0"

print("add:", c.post("/api/cart", json={
    "item": {"productId": product, "quantity": 2},
    "userId": "u1",
}).json())

print("cart:", c.get("/api/cart", params={"user_id": "u1"}).json())

payload = {
    "userId": "u1",
    "userCurrency": "USD",
    "address": {
        "streetAddress": "1 Main St",
        "city": "Testville",
        "state": "CA",
        "country": "US",
        "zipCode": "90210",
    },
    "email": "test@example.com",
    "creditCard": {
        "creditCardNumber": "4111111111111111",
        "creditCardCvv": 123,
        "creditCardExpirationYear": 2030,
        "creditCardExpirationMonth": 1,
    },
}

first = c.post("/api/checkout", json=payload)
print("first checkout:", first.status_code, sorted(first.json()))

second = c.post("/api/checkout", json=payload)
print("second checkout:", second.status_code, second.json())
PY
```

The second checkout should return `400` because the first one emptied the cart.
The script is intentionally explicit; the shapes are the point.

Ask the agent:

```sh
curl -s localhost:8010/prompt \
  -H 'content-type: application/json' \
  -d '{"message":"Show all available products in the store.","history":[]}' \
  | jq '.response.messages[-1].content'
```

## What to observe

- The final answer lists the same ten products the shop returns.
- The agent chose `list_products`; the shop log shows `GET /api/products`.
- `GET /api/cart` includes `product` on each item; `POST /api/cart` does not.
- `DELETE /api/cart` returns `204`, and `empty_cart` converts that into a small
  success object for the model.
- The second checkout fails with `400`, and the `checkout` tool turns that into
  a string that tells the model to add an item first.

## Exercises

1. Add two of a product, then three more of the same product. What quantity does
   `GET /api/cart` report? Why?
2. Call `DELETE /api/cart` twice for the same user. What status does the second
   call return? Is the operation idempotent?
3. Checkout, then checkout again. What does the `checkout` tool return to the
   model? Where in `domain/tools.py` is that message built?
4. Stop only the shop API:

   ```sh
   docker compose -f platform/compose.core.yaml stop shop-api
   ```

   Ask the products prompt again. Does the agent still answer? Why is that
   dangerous to rely on? Restart it with:

   ```sh
   docker compose -f platform/compose.core.yaml start shop-api
   ```
5. Add a `GET /api/products/count` endpoint and a matching `product_count` tool.
   What has to change for the model to see it? (Hint: guide 3.)

## Checkpoint

- Why does `get_supported_currencies` return a bare list?
- What does the model see when a tool fails, and what would it see if the tool
  raised instead?
- Which span wraps the whole agent run, and which decorator creates it?
- Where would you add a new endpoint and its tool?

## Next

[Guide 1 — LLM boundary and replay](./01-llm-boundary-and-replay.md)
