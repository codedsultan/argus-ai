from __future__ import annotations

import os

from langchain.tools import tool

from domain.tools import (
    add_to_cart,
    checkout,
    empty_cart,
    get_ads,
    get_product,
    get_recommendations,
    get_shipping_quote,
    get_supported_currencies,
    list_products,
)

PUBLIC_MODE = os.getenv("PUBLIC_MODE", "false").lower() == "true"


def build_tools(public_mode: bool = PUBLIC_MODE):
    """Build the tool surface for the current profile.

    The public profile is read-only. Cart mutation and checkout stay in the
    local development profile, where an operator can reach them intentionally.
    """
    read_only = [
        tool(get_ads),
        tool(get_product),
        tool(get_recommendations),
        tool(get_shipping_quote),
        tool(get_supported_currencies),
        tool(list_products),
    ]
    if public_mode:
        return read_only
    return [
        tool(add_to_cart),
        tool(checkout),
        tool(empty_cart),
        *read_only,
    ]


TOOLS = build_tools()
