from __future__ import annotations

import asyncio
import os
import time

import httpx
from langchain_openai import ChatOpenAI

from .flags import get_int_feature_flag
from .replay import USE_VCR, VCR


class ChatLLM(ChatOpenAI):
    def __init__(self, **kwargs):
        model_name = os.getenv("LLM_MODEL", "azure/gpt-5.5")
        llm_tls_verify = os.getenv("LLM_TLS_VERIFY", "true").lower() == "true"
        cassette_name = f"{model_name.replace('/', '_')}_cassette.yaml"

        if "http_async_client" not in kwargs:
            kwargs["http_async_client"] = httpx.AsyncClient(verify=llm_tls_verify)
        kwargs.setdefault("openai_api_base", os.getenv("LLM_BASE_URL") or None)
        kwargs.setdefault("model", model_name)

        api_key = os.getenv("API_KEY")
        if not api_key:
            api_key = "sk-dummy"
        kwargs.setdefault("api_key", api_key)

        super().__init__(**kwargs)

        object.__setattr__(self, "_use_vcr", USE_VCR)
        object.__setattr__(self, "_cassette_name", cassette_name)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        delay_ms = get_int_feature_flag("aiSlowResponse")
        if delay_ms:
            time.sleep(delay_ms / 1000)
        if getattr(self, "_use_vcr", False):
            with VCR.use_cassette(self._cassette_name):
                return super()._generate(messages, stop, run_manager, **kwargs)
        return super()._generate(messages, stop, run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        delay_ms = get_int_feature_flag("aiSlowResponse")
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        if getattr(self, "_use_vcr", False):
            with VCR.use_cassette(self._cassette_name):
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)
