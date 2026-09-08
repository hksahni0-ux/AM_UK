"""
Shared LLM call shim — every agent routes text/tool completions through here
instead of an Anthropic client directly, so the active provider is a single
config flag (LLM_PROVIDER in .env) rather than a per-file edit. NVIDIA's NIM
endpoint is OpenAI-compatible, so it's called via plain `requests` (no extra
SDK dependency) rather than an OpenAI/Anthropic client.
"""

import json
import logging
import time

import requests
import anthropic

from config.settings import (
    LLM_PROVIDER, LLM_MODELS,
    ANTHROPIC_API_KEY, NVIDIA_API_KEY, NVIDIA_API_BASE,
)

log = logging.getLogger(__name__)

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30)

_NVIDIA_HEADERS = {
    "Authorization": f"Bearer {NVIDIA_API_KEY}",
    "Content-Type": "application/json",
}


def _model_for(task: str) -> str:
    return LLM_MODELS[LLM_PROVIDER][task]


# NVIDIA's NIM models (Nemotron, Muse Glimmer) reason internally before answering —
# that reasoning eats into max_tokens and lands in a separate `reasoning_content`
# field, so a budget sized for Claude's direct-answer output leaves no room for the
# actual answer and the call comes back truncated with content=null. Pad it here
# rather than at every call site.
_NVIDIA_REASONING_BUFFER = 3000

# "ultra" is NVIDIA's heaviest tier and the one that's shown repeated 503s
# (backend overloaded) on write-heavy tasks — on a 503, step down once to
# "super" (already proven stable in production for role_match/left_company_check)
# rather than losing the whole 15-minute cycle for that contact.
_NVIDIA_FALLBACK_MODEL = {
    "nvidia/nemotron-3-ultra-550b-a55b": "nvidia/nemotron-3-super-120b-a12b",
}


def _nvidia_post(payload: dict) -> dict:
    """POST to the NVIDIA NIM chat/completions endpoint.

    Retries once on 429/timeout against the same model. On a 503 (backend
    overloaded) for a model with a configured fallback, retries once more
    against that fallback model instead of failing the call outright.
    """
    model = payload["model"]
    fallback_model = _NVIDIA_FALLBACK_MODEL.get(model)
    used_fallback = False
    attempt = 0

    while True:
        try:
            resp = requests.post(
                f"{NVIDIA_API_BASE}/chat/completions",
                headers=_NVIDIA_HEADERS,
                json=payload,
                timeout=180,
            )
        except requests.exceptions.Timeout:
            if attempt == 0:
                attempt += 1
                continue
            raise

        if resp.status_code == 429 and attempt == 0:
            time.sleep(2)
            attempt += 1
            continue

        if resp.status_code == 503 and fallback_model and not used_fallback:
            log.warning("NVIDIA 503 on %s — retrying once with fallback %s", model, fallback_model)
            payload = {**payload, "model": fallback_model}
            used_fallback = True
            attempt += 1
            continue

        resp.raise_for_status()
        return resp.json()


def complete(task: str, prompt: str, max_tokens: int = 1024) -> str:
    """Single-turn text completion for `task`, using whichever provider is active."""
    model = _model_for(task)
    if LLM_PROVIDER == "anthropic":
        msg = _anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    elif LLM_PROVIDER == "nvidia":
        data = _nvidia_post({
            "model": model,
            "max_tokens": max_tokens + _NVIDIA_REASONING_BUFFER,
            "messages": [{"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False},
        })
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length" or choice["message"].get("content") is None:
            log.warning("NVIDIA truncation — usage=%s", data.get("usage"))
            raise ValueError("NVIDIA response truncated before finishing (reasoning overhead exceeded budget)")
        return choice["message"]["content"].strip()
    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER}")


def complete_tool(task: str, prompt: str, tool_schema: dict, max_tokens: int = 2000) -> dict:
    """
    Forced tool-call completion — returns the tool's parsed input/arguments dict.
    `tool_schema` is always given in Anthropic's {name, description, input_schema}
    shape; converted to OpenAI function-calling shape internally for NVIDIA.
    Raises ValueError if the response was truncated before finishing the call.
    """
    model = _model_for(task)
    name = tool_schema["name"]

    if LLM_PROVIDER == "anthropic":
        msg = _anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": name},
            messages=[{"role": "user", "content": prompt}],
        )
        if msg.stop_reason == "max_tokens":
            raise ValueError("Response was truncated by max_tokens — retry rather than use a partial result")
        for block in msg.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError(f"No tool_use block returned for {name}")

    elif LLM_PROVIDER == "nvidia":
        data = _nvidia_post({
            "model": model,
            "max_tokens": max_tokens + _NVIDIA_REASONING_BUFFER,
            "tools": [{
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool_schema["description"],
                    "parameters": tool_schema["input_schema"],
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": name}},
            "messages": [{"role": "user", "content": prompt}],
            "chat_template_kwargs": {"enable_thinking": False},
        })
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            log.warning("NVIDIA truncation — usage=%s", data.get("usage"))
            raise ValueError("Response was truncated by max_tokens — retry rather than use a partial result")
        tool_calls = choice["message"].get("tool_calls") or []
        if not tool_calls:
            raise ValueError(f"No tool call returned for {name}")
        return json.loads(tool_calls[0]["function"]["arguments"])

    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER}")
