"""OpenAI-compatible Chat Completions transport, independent of any UI."""
import json
import time

import httpx

from .analysis import ModelFailure


class ChatModel:
    def __init__(self, client, config, budget):
        self.client, self.config, self.budget = client, config, budget

    def complete(self, messages, tools, finish_only=False):
        llm = self.config["llm"]
        maximum = self.config.get("max_output_tokens", 1600)
        body = {"model": llm["model"], "messages": messages, "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": "finish_research"}} if finish_only else "auto"}
        token_parameter = llm.get("token_parameter", "max_tokens")
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ModelFailure("模型输出长度参数必须为 max_tokens 或 max_completion_tokens")
        body[token_parameter] = maximum
        reservation = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + maximum + 256
        self.budget.reserve(reservation)
        try:
            request = self.client.build_request("POST", llm["base_url"].rstrip("/") + "/chat/completions", json=body,
                                                timeout=min(60, max(.1, self.budget.deadline - time.monotonic())))
            request.headers.pop("Cookie", None)
            request.headers["Authorization"] = "Bearer " + llm["api_key"]
            response = self.client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                if response.status_code >= 400:
                    raise ModelFailure(f"模型接口返回 HTTP {response.status_code}；请检查地址、模型名称、权限及工具调用支持")
                if response.is_redirect:
                    raise ModelFailure("模型接口发生重定向，已停止转发凭据")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 524288 or time.monotonic() >= self.budget.deadline:
                        raise ModelFailure("模型响应超过大小或时间限制")
            finally:
                response.close()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("expected object")
            message = value["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("expected message")
            usage = value.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("expected usage")
            self.budget.actual += max(0, int(usage.get("total_tokens", 0)))
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 8:
                raise ValueError("invalid tool call list")
            normalized = []
            for call in calls:
                function = call["function"]
                if call.get("type") != "function" or not isinstance(call["id"], str) or not 1 <= len(call["id"]) <= 200:
                    raise ValueError("invalid tool call")
                if not isinstance(function["name"], str) or not isinstance(function["arguments"], str) or len(function["arguments"]) > 30000:
                    raise ValueError("invalid tool arguments")
                normalized.append({"id": call["id"], "type": "function", "function": {"name": function["name"], "arguments": function["arguments"]}})
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("invalid message content")
            if not normalized and not content:
                raise ValueError("empty model message")
            return {"role": "assistant", "content": content, **({"tool_calls": normalized} if normalized else {})}
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelFailure("模型响应无法使用：" + type(exc).__name__) from None
