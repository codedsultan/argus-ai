# Shop API

A small in-memory shop backend for the agent. It keeps the public demo
self-contained and deterministic.

FastAPI serves the OpenAPI contract at `/openapi.json` and interactive docs at
`/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness |
| GET | `/api/products` | List products |
| GET | `/api/products/{id}` | Product detail |
| GET | `/api/currency` | Supported currencies |
| GET | `/api/data?contextKeys=` | Promotional ads |
| GET | `/api/recommendations?productIds=` | Recommended product IDs |
| GET | `/api/cart?user_id=` | Read a cart |
| POST | `/api/cart` | Add an item |
| DELETE | `/api/cart` | Empty a cart |
| GET | `/api/shipping` | Shipping quote |
| POST | `/api/checkout` | Create an order |

State is in memory and resets when the process restarts. There is no auth and no
persistence; this is a learning fixture, not a service.
