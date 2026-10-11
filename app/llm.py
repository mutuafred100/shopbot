"""Talks to the chosen AI provider. Claude is called natively; OpenAI, Google Gemini, Z.ai (GLM) and any other
'OpenAI-compatible' service share one code path. Only plain HTTP is used, so there is nothing extra to install."""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

from . import settings

log = logging.getLogger("shop.llm")
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "zai": "ZAI_API_KEY"}


class LLMError(Exception):
    pass


def provider() -> str:
    p = settings.get("llm_provider")
    return p if p in settings.LLM_PROVIDERS else "anthropic"


def api_key() -> str:
    return (settings.get("llm_api_key") or (settings.get("anthropic_api_key") if provider() == "anthropic" else "")
            or os.getenv(ENV_KEYS.get(provider(), ""), ""))


def model() -> str:
    legacy = settings.get("claude_model") if provider() == "anthropic" else ""
    return settings.get("llm_model") or legacy or settings.LLM_PROVIDERS[provider()]["model"]


def base_url() -> str:
    return (settings.get("llm_base_url") if provider() == "custom" else settings.LLM_PROVIDERS[provider()]["base"]).rstrip("/")


def _friendly(status: int, text: str) -> str:
    hint = {401: "The API key was rejected - check it.", 403: "The API key was rejected or has no access - check it.",
            404: "Address or model name not found - check the Model name in Settings.",
            429: "Too many requests or no credit left on the AI account."}.get(status, "")
    return f"AI service error {status}. {hint} {text[:250]}".strip()


def _post(url: str, headers: dict, payload: dict) -> dict:
    last: Exception | None = None
    for attempt in range(2):
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=60)
        except httpx.HTTPError as e:
            last = LLMError(f"Could not reach the AI service: {e}")
            time.sleep(1.5)
            continue
        if r.status_code in (429, 500, 502, 503, 529) and attempt == 0:
            time.sleep(2)
            continue
        if r.status_code >= 400:
            raise LLMError(_friendly(r.status_code, r.text))
        return r.json()
    raise last or LLMError("The AI service did not answer.")


def _tool_json(out: dict) -> str:
    return json.dumps(out, ensure_ascii=False)


# ---------------- Claude (Anthropic) ----------------
def _run_anthropic(system, messages, tools, execute, rounds):
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
    headers = {"x-api-key": api_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for _ in range(rounds):
        payload = {"model": model(), "max_tokens": 800, "system": system, "messages": msgs}
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools]
        data = _post("https://api.anthropic.com/v1/messages", headers, payload)
        blocks = data.get("content", [])
        if data.get("stop_reason") != "tool_use":
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        msgs.append({"role": "assistant", "content": blocks})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b["id"], "content": _tool_json(execute(b["name"], b.get("input") or {}))}
            for b in blocks if b.get("type") == "tool_use"]})
    return ""


# ---------------- OpenAI, Gemini, Z.ai and other OpenAI-compatible services ----------------
def _openai_tools(tools):
    out = []
    for t in tools:
        schema = json.loads(json.dumps(t["input_schema"]))
        if not schema.get("properties"):  # some providers (Gemini) reject tools with no parameters
            schema["properties"] = {"unused": {"type": "string", "description": "leave empty"}}
        out.append({"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": schema}})
    return out


def _run_openai(system, messages, tools, execute, rounds):
    url = base_url() + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in messages]
    for _ in range(rounds):
        payload = {"model": model(), "messages": msgs}
        if tools:
            payload["tools"] = _openai_tools(tools)
        data = _post(url, headers, payload)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected answer from the AI service: {str(data)[:250]}")
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        # keep each tool call exactly as received (Gemini 3 needs its hidden 'extra_content' signature sent back)
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = {"role": "tool", "tool_call_id": c.get("id", ""), "content": _tool_json(execute(fn.get("name", ""), args))}
            if provider() == "gemini":
                result["name"] = fn.get("name", "")
            msgs.append(result)
    return ""


def run(system: str, messages: list[dict], tools: list[dict], execute, rounds: int = 8) -> str:
    """messages: [{'role': 'user'|'assistant', 'content': text}], ending with the new customer message.
    tools: [{'name','description','input_schema'}]. execute(name, args) -> dict. Returns the final reply text."""
    if not api_key():
        raise LLMError("No AI API key set.")
    if provider() == "custom" and not (base_url() and model()):
        raise LLMError("For 'Other' providers you must set the Base URL and Model name.")
    fn = _run_anthropic if provider() == "anthropic" else _run_openai
    return fn(system, messages, tools, execute, rounds)


def test_connection() -> tuple[bool, str]:
    try:
        reply = run("You are a connection test.", [{"role": "user", "content": "Reply with the single word OK."}], [], lambda n, a: {})
        return True, f"Connected to {settings.LLM_PROVIDERS[provider()]['label']} using model '{model()}'. It answered: {reply[:60] or '(empty)'}"
    except LLMError as e:
        return False, str(e)
    except Exception as e:  # never crash the settings page
        return False, f"Unexpected problem: {e}"
