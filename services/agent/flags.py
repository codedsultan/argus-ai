from __future__ import annotations

import os

_ENV_FLAGS = {
    "aiSlowResponse": "AI_SLOW_RESPONSE_MS",
    "aiRunawayAgent": "AI_RUNAWAY_ITERATIONS",
}


def get_int_feature_flag(flag_name: str, default: int = 0) -> int:
    env_name = _ENV_FLAGS.get(flag_name)
    if env_name is None:
        return default

    raw = os.getenv(env_name)
    if raw is None or raw == "":
        return default
    return int(raw)
