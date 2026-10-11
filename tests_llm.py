"""Offline test of the AI provider layer with a fake network. Run: python tests_llm.py"""
import json, os, sys, tempfile, types

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sent = []
script = []  # queue of fake responses


class Resp:
    def __init__(self, status, data): self.status_code, self._d, self.text = status, data, json.dumps(data)
    def json(self): return self._d


fake = types.ModuleType("httpx")
fake.HTTPError = type("HTTPError", (Exception,), {})
def post(url, headers=None, json=None, timeout=None):
    import copy
    sent.append((url, headers, copy.deepcopy(json)))
    return script.pop(0)
fake.post = post
sys.modules["httpx"] = fake

from app import llm, settings

executed = []
def execute(name, args):
    executed.append((name, args)); return {"ok": True, "echo": args}

TOOLS = [{"name": "view_cart", "description": "d", "input_schema": {"type": "object", "properties": {}}},
         {"name": "add", "description": "d", "input_schema": {"type": "object", "properties": {"q": {"type": "integer", "minimum": 1}}, "required": ["q"]}}]
MSGS = [{"role": "user", "content": "hi"}]

# ---- Claude
settings.save({"llm_provider": "anthropic", "llm_api_key": "k1"})
script[:] = [Resp(200, {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "a1", "name": "add", "input": {"q": 2}}]}),
             Resp(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done 👍"}]})]
assert llm.run("sys", MSGS, TOOLS, execute) == "Done 👍"
url, headers, body = sent[0]
assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "k1" and body["model"] == "claude-sonnet-5-5"
assert sent[1][2]["messages"][-1]["content"][0]["tool_use_id"] == "a1" and executed[-1] == ("add", {"q": 2})

# ---- OpenAI
sent.clear()
settings.save({"llm_provider": "openai", "llm_api_key": "k2"})
script[:] = [Resp(200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "add", "arguments": "{\"q\": 3}"}}]}}]}),
             Resp(200, {"choices": [{"message": {"role": "assistant", "content": "Added 3"}}]})]
assert llm.run("sys", MSGS, TOOLS, execute) == "Added 3"
url, headers, body = sent[0]
assert url == "https://api.openai.com/v1/chat/completions" and headers["Authorization"] == "Bearer k2"
assert body["messages"][0] == {"role": "system", "content": "sys"}
empty_tool = [t for t in body["tools"] if t["function"]["name"] == "view_cart"][0]["function"]["parameters"]
assert empty_tool["properties"], "empty-parameter tools get a dummy property"
tool_msg = sent[1][2]["messages"][-1]
assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "name" not in tool_msg
assert executed[-1] == ("add", {"q": 3})

# ---- Gemini keeps the hidden signature on tool calls and names tool results
sent.clear()
settings.save({"llm_provider": "gemini", "llm_api_key": "k3"})
call = {"id": "g1", "type": "function", "function": {"name": "view_cart", "arguments": "{}"},
        "extra_content": {"google": {"thought_signature": "SIG"}}}
script[:] = [Resp(200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]}),
             Resp(200, {"choices": [{"message": {"role": "assistant", "content": "Your cart is empty."}}]})]
assert llm.run("sys", MSGS, TOOLS, execute) == "Your cart is empty."
assert sent[0][0] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
assert sent[0][2]["model"] == "gemini-flash-latest"
second = sent[1][2]["messages"]
assert second[-2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG"
assert second[-1]["name"] == "view_cart"

# ---- Z.ai and custom
sent.clear()
settings.save({"llm_provider": "zai", "llm_api_key": "k4"})
script[:] = [Resp(200, {"choices": [{"message": {"content": "hello"}}]})]
assert llm.run("s", MSGS, [], execute) == "hello"
assert sent[0][0] == "https://api.z.ai/api/paas/v4/chat/completions" and "tools" not in sent[0][2]
settings.save({"llm_provider": "custom", "llm_base_url": "https://api.deepseek.com/v1/", "llm_model": "deepseek-chat"})
script[:] = [Resp(200, {"choices": [{"message": {"content": "hi"}}]})]
llm.run("s", MSGS, [], execute)
assert sent[-1][0] == "https://api.deepseek.com/v1/chat/completions" and sent[-1][2]["model"] == "deepseek-chat"
settings.save({"llm_model": ""}); 
try: llm.run("s", MSGS, [], execute); raise SystemExit("custom without model should fail")
except llm.LLMError: pass

# ---- errors are readable; connection test never crashes
settings.save({"llm_provider": "openai", "llm_model": "bad-model"})
script[:] = [Resp(404, {"error": "model_not_found"})]
ok, msg = llm.test_connection()
assert not ok and "model name" in msg.lower(), msg
script[:] = [Resp(401, {"error": "bad key"})]
assert "key was rejected" in llm.test_connection()[1]
script[:] = [Resp(200, {"choices": [{"message": {"content": "OK"}}]})]
ok, msg = llm.test_connection(); assert ok and "OK" in msg
print("LLM LAYER OK")
