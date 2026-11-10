from __future__ import annotations

import json
import logging
import os
from difflib import SequenceMatcher

import vcr

logging.getLogger("vcr").setLevel(logging.ERROR)

CASSETTE_DIR = os.getenv("VCR_CASSETTE_DIR", "evals/cassettes")
USE_VCR = os.getenv("USE_VCR", "true").lower() == "true"
VCR_MATCH_THRESHOLD = float(os.getenv("VCR_MATCH_THRESHOLD", "0.85"))
ALLOWED_RECORD_MODES = {"once", "new_episodes", "none", "all"}


def record_mode(value: str | None = None) -> str:
    """Return a validated VCR record mode.

    Public deployments set ``VCR_RECORD_MODE=none`` so a cassette miss raises
    instead of attempting a provider request. The local lab keeps the
    ``new_episodes`` default so an operator can extend a cassette.
    """
    mode = (
        value
        if value is not None
        else os.getenv("VCR_RECORD_MODE", "new_episodes")
    ).strip().lower()
    if mode not in ALLOWED_RECORD_MODES:
        raise ValueError(
            f"VCR_RECORD_MODE must be one of {sorted(ALLOWED_RECORD_MODES)}"
        )
    return mode


VCR_RECORD_MODE = record_mode()

try:
    import vcr.stubs.httpx_stubs as _httpx_stubs

    _original_deserialize_response = _httpx_stubs._deserialize_response
except (ImportError, AttributeError):
    _httpx_stubs = None
    _original_deserialize_response = None


def _patched_deserialize_response(vcr_response, httpx_module):
    body = vcr_response.get("body", {}).get("string")
    if isinstance(body, str):
        vcr_response["body"]["string"] = body.encode("utf-8")
    return _original_deserialize_response(vcr_response, httpx_module)


if USE_VCR and _original_deserialize_response is not None:
    _httpx_stubs._deserialize_response = _patched_deserialize_response


def normalize_body(request):
    try:
        if request.body:
            encoding = "utf-8"
            if hasattr(request.body, "decode"):
                data = json.loads(request.body.decode(encoding))
            else:
                data = json.loads(request.body)
            request.body = json.dumps(data, sort_keys=True).encode(encoding)
            request.headers = {}
            request.uri = "https://vcr.local/"
            request.method = "POST"
    except Exception:
        pass
    return request


def clean_response(response):
    status_code = response.get("status", {}).get("code")
    if status_code is not None and not (200 <= int(status_code) < 300):
        logging.warning("VCR: not recording response (HTTP %s)", status_code)
        return None

    response["headers"] = {}
    try:
        body = response.get("body", {}).get("string")
        if isinstance(body, bytes):
            body = body.decode("utf-8")

        data = json.loads(body)

        if isinstance(data, dict) and "error" in data and "choices" not in data:
            logging.warning("VCR: not recording response (provider error payload)")
            return None

        if "id" in data:
            data["id"] = "chatcmpl-fixture"
        if "created" in data:
            data["created"] = 0
        for key in [
            "system_fingerprint",
            "usage",
            "prompt_filter_results",
            "service_tier",
        ]:
            data.pop(key, None)

        for choice in data.get("choices", []):
            choice.pop("provider_specific_fields", None)
            message = choice.get("message", {})
            message.pop("provider_specific_fields", None)
            message.pop("annotations", None)

        response["body"]["string"] = json.dumps(data, sort_keys=True)
    except Exception:
        pass
    return response


def _parse_body(request):
    body = getattr(request, "body", None)
    if body is None:
        return None
    if hasattr(body, "decode"):
        try:
            body = body.decode("utf-8")
        except Exception:
            return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _normalize_message(message):
    if isinstance(message, dict) and message.get("role") == "tool":
        normalized = dict(message)
        normalized["content"] = "<tool-result>"
        return normalized
    return message


def _stringify(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except Exception:
        return str(value)


def body_similarity(request_a, request_b):
    """Return a similarity score in [0, 1] between two LLM request bodies."""
    data_a = _parse_body(request_a)
    data_b = _parse_body(request_b)

    messages_a = data_a.get("messages") if isinstance(data_a, dict) else None
    messages_b = data_b.get("messages") if isinstance(data_b, dict) else None

    if not isinstance(messages_a, list) or not isinstance(messages_b, list):
        return SequenceMatcher(None, _stringify(data_a), _stringify(data_b)).ratio()

    if len(messages_a) != len(messages_b):
        return 0.0

    if not messages_a:
        return 1.0

    normalized_a = [_normalize_message(message) for message in messages_a]
    normalized_b = [_normalize_message(message) for message in messages_b]

    full_ratio = SequenceMatcher(
        None, _stringify(normalized_a), _stringify(normalized_b)
    ).ratio()
    last_ratio = SequenceMatcher(
        None, _stringify(normalized_a[-1]), _stringify(normalized_b[-1])
    ).ratio()
    return 0.4 * full_ratio + 0.6 * last_ratio


def similar_body_matcher(request_a, request_b):
    score = body_similarity(request_a, request_b)
    if score >= VCR_MATCH_THRESHOLD:
        return
    raise AssertionError(
        f"request bodies differ beyond threshold "
        f"(score={score:.4f} < {VCR_MATCH_THRESHOLD:.4f})"
    )


VCR = vcr.VCR(
    cassette_library_dir=CASSETTE_DIR,
    record_mode=VCR_RECORD_MODE,
    serializer="yaml",
    path_transformer=vcr.VCR.ensure_suffix(".yaml"),
    filter_headers=["authorization", "x-api-key", "api-key"],
    match_on=["similar_body"],
    before_record_request=normalize_body,
    before_record_response=clean_response,
    decode_compressed_response=True,
)
VCR.register_matcher("similar_body", similar_body_matcher)


def select_best_match(requests, play_counts, request):
    """Return ``(index, score)`` of the best unplayed recorded request.

    Returns ``(None, -1.0)`` when no unplayed request meets the threshold. The
    function is pure so the selection rule can be tested without VCR.
    """
    best_idx = None
    best_score = -1.0
    for idx, recorded in enumerate(requests):
        if play_counts[idx] > 0:
            continue
        try:
            score = body_similarity(recorded, request)
        except Exception:
            score = 0.0
        if score >= VCR_MATCH_THRESHOLD and score > best_score:
            best_score = score
            best_idx = idx
    return best_idx, best_score


def _install_best_match_playback():
    from vcr.cassette import Cassette
    from vcr.errors import UnhandledHTTPRequestError

    def play_response(self, request):
        best_idx, best_score = select_best_match(
            self.requests, self.play_counts, request
        )

        if best_idx is None:
            raise UnhandledHTTPRequestError(
                f"No unplayed cassette request within threshold "
                f"({VCR_MATCH_THRESHOLD:.4f}) for {request}"
            )

        self.play_counts[best_idx] += 1
        logging.info(
            "VCR best-match playback: entry %d (score=%.4f)", best_idx, best_score
        )
        return self.responses[best_idx]

    Cassette.play_response = play_response


if USE_VCR:
    try:
        _install_best_match_playback()
    except Exception as exc:
        logging.error("Could not install best-match VCR playback: %s", exc)
