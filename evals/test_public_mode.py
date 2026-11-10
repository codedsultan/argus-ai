from __future__ import annotations

import pytest

from services.agent.replay import record_mode
from services.agent.tools_builtin import build_tools


def _names(public_mode: bool) -> set[str]:
    return {getattr(item, "name", "") for item in build_tools(public_mode)}


def test_public_profile_excludes_mutating_and_cart_tools():
    names = _names(True)

    assert "list_products" in names
    assert "get_supported_currencies" in names
    assert "get_ads" in names
    assert "checkout" not in names
    assert "add_to_cart" not in names
    assert "empty_cart" not in names
    assert "get_cart" not in names


def test_full_profile_keeps_checkout():
    names = _names(False)

    assert "checkout" in names
    assert "add_to_cart" in names
    assert "empty_cart" in names


def test_record_mode_accepts_none():
    assert record_mode("none") == "none"


def test_record_mode_rejects_unknown_value():
    with pytest.raises(ValueError):
        record_mode("record")
