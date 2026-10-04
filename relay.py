#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode Go relay
=================

让 Claude Code (Anthropic Messages 协议) 和 Codex (OpenAI Responses 协议)
共用 OpenCode Go 订阅里的 OpenAI-compatible 模型（默认 deepseek-v4-flash）。

协议转换:
  POST /v1/messages        (Anthropic)   -> 上游 /v1/chat/completions (OpenAI)
  POST /v1/responses       (Responses)   -> 上游 /v1/chat/completions (OpenAI)
  POST /v1/chat/completions (OpenAI)     -> 上游直通 (仅改模型/鉴权)
  GET  /v1/models                        -> 模型列表
  GET  /healthz                          -> 健康检查

环境变量:
  OPENCODE_GO_API_KEY   可选, 设置后所有请求共用该 key; 留空则每个请求必须自带 key
  RELAY_TOKEN           可选, 强烈建议公网部署时设置, 客户端用它鉴权
  DEFAULT_MODEL         默认 deepseek-v4-flash
  UPSTREAM_BASE         默认 https://opencode.ai/zen/go/v1
  HOST / PORT           默认 0.0.0.0:8787
  STREAM_OPTIONS        默认 1 (向上游请求 usage), 上游报错可设 0
  UPSTREAM_UA           默认浏览器 UA, 绕过上游 Cloudflare 等按 UA 拦截
  MAX_CONNECTIONS       默认 64, 限制并发连接数, 防连接洪水
  CLIENT_TIMEOUT        默认 60 (秒), 客户端连接读超时, 防 slowloris

仅用 Python 标准库, 无第三方依赖。Python 3.9+。
"""

import hmac
import json
import os
import signal
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://opencode.ai/zen/go/v1").rstrip("/")
GO_KEY = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "deepseek-v4-flash").strip()
MODELS_EXTRA = [m.strip() for m in os.environ.get("MODELS_EXTRA", "").split(",") if m.strip()]
HOST = os.environ.get("HOST", "0.0.0.0")


def _env_number(name, default, cast=int):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        sys.stderr.write("ERROR: %s must be a number, got %r\n" % (name, raw))
        sys.exit(1)


PORT = _env_number("PORT", 8787)
REQUEST_TIMEOUT = _env_number("REQUEST_TIMEOUT", 600, cast=float)
USE_STREAM_OPTIONS = os.environ.get("STREAM_OPTIONS", "1") != "0"
MAX_BODY = 64 * 1024 * 1024  # 64 MB
MAX_CONNECTIONS = _env_number("MAX_CONNECTIONS", 64)
CLIENT_TIMEOUT = _env_number("CLIENT_TIMEOUT", 60)

# Anthropic 原生透传(2026-10-03 实测):上游 Zen 有 /zen/go/v1/messages(Anthropic 格式, 只认 x-api-key),
# 直接转发原始 body + 原样回传 SSE 字节 → prompt caching(cache_read_input_tokens)、thinking、tool_use、
# anthropic-beta 全部原生语义,不再走"翻译成 OpenAI"那条路。设 ANTHROPIC_PASSTHROUGH=0 可退回旧翻译模式。
ANTHROPIC_PASSTHROUGH = os.environ.get("ANTHROPIC_PASSTHROUGH", "1") != "0"

# Cloudflare 等上游会按请求签名拦截 urllib 默认的 Python UA，这里给一个浏览器 UA 兜底。
UPSTREAM_UA = os.environ.get(
    "UPSTREAM_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
).strip()


def _uuid(prefix=""):
    return prefix + uuid.uuid4().hex[:24]


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 上游请求
# ---------------------------------------------------------------------------

def upstream_request(path, payload, headers=None, api_key=None):
    """POST 到 OpenCode Go 上游, 返回 http.client.HTTPResponse (可流式读)。

    api_key 优先取请求级 key, 否则回退服务器级 GO_KEY (OPENCODE_GO_API_KEY)。
    """
    req = urllib.request.Request(
        UPSTREAM_BASE + path,
        data=json_bytes(payload),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + (api_key or GO_KEY))
    req.add_header("User-Agent", UPSTREAM_UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


def upstream_anthropic_request(path, raw_body, headers=None, api_key=None):
    """透传模式:原始 Anthropic 请求打到上游 Anthropic 原生端点(/messages)。

    该端点只认 x-api-key;送 Authorization: Bearer 会回 401 AuthError("Missing API key")。
    """
    req = urllib.request.Request(UPSTREAM_BASE + path, data=raw_body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", (api_key or GO_KEY))
    req.add_header("User-Agent", UPSTREAM_UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


_TIKTOKEN_ENC = None


def _tiktoken_enc():
    """惰性加载 tiktoken o200k_base;不可用返回 None(退字符估算)。"""
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is False:
        return None
    if _TIKTOKEN_ENC is None:
        try:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("o200k_base")
        except Exception:
            _TIKTOKEN_ENC = False
            return None
    return _TIKTOKEN_ENC


def estimate_input_tokens(payload):
    """本地估算 Anthropic 请求的 input_tokens(上游没有 count_tokens 端点 → 只能近似)。

    近似偏差来自分词器不同(DeepSeek 自家 BPE vs o200k_base),所以是估算值,不是权威计数;
    Claude Code 用它做上下文预算/压缩判断,量级正确即可。
    """
    parts = []
    sysblk = (payload or {}).get("system")
    if isinstance(sysblk, str):
        parts.append(sysblk)
    elif isinstance(sysblk, list):
        for b in sysblk:
            if isinstance(b, dict) and b.get("text"):
                parts.append(str(b["text"]))
    for m in (payload or {}).get("messages") or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    parts.append(str(b.get("text", "")))
                elif t == "tool_result":
                    parts.append(json.dumps(b.get("content"), ensure_ascii=False))
                elif t == "tool_use":
                    parts.append(json.dumps(b.get("input"), ensure_ascii=False))
                elif t == "image":
                    parts.append("x" * 1600)  # 图像按固定量粗估
    for t in (payload or {}).get("tools") or []:
        parts.append(json.dumps(t, ensure_ascii=False))
    blob = "\n".join(p for p in parts if p)
    enc = _tiktoken_enc()
    if enc is None:
        return max(1, int(len(blob) / 3.6))
    try:
        return max(1, len(enc.encode(blob)))
    except Exception:
        return max(1, int(len(blob) / 3.6))


def iter_sse_lines(resp):
    """按行读取上游 SSE 响应, 逐行 yield (保留原字节)。"""
    buf = b""
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line
    if buf:
        yield buf


def parse_sse_json(line):
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    data = line[5:].strip()
    if not data or data == b"[DONE]":
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Anthropic Messages -> OpenAI Chat Completions
# ---------------------------------------------------------------------------

def _extract_system_blocks(system):
    if isinstance(system, str):
        return system
    parts = []
    for b in system or []:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]))
    return "\n\n".join(parts)


def _image_block_to_openai(block):
    src = block.get("source") or {}
    if src.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": src.get("url", "")}}
    if src.get("type") == "base64":
        url = "data:%s;base64,%s" % (src.get("media_type", "image/png"), src.get("data", ""))
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _append_assistant(messages, text, tool_calls):
    if not text and not tool_calls:
        return
    if messages and messages[-1].get("role") == "assistant":
        last = messages[-1]
        if text:
            cur = last.get("content")
            if not cur:
                last["content"] = text
            elif isinstance(cur, str):
                last["content"] = cur + "\n\n" + text
        if tool_calls:
            last.setdefault("tool_calls", []).extend(tool_calls)
            last.setdefault("content", None)
        return
    msg = {"role": "assistant"}
    if text:
        msg["content"] = text
    if tool_calls:
        msg["tool_calls"] = tool_calls
    messages.append(msg)


def _map_tool_choice(tc):
    if isinstance(tc, str):
        if tc == "any":
            return "required"
        return tc if tc in ("auto", "none", "required") else "auto"
    if not isinstance(tc, dict):
        return "auto"
    t = tc.get("type")
    if t == "any":
        return "required"
    if t == "tool":
        name = tc.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
        return "required"
    return "auto"


def anthropic_to_openai(body):
    """把 Claude Code 的 /v1/messages 请求翻译成 OpenAI chat.completions。"""
    # 透传客户端指定的模型 (例如 glm-5.2 / deepseek-v4-pro), 未指定才回退默认
    out = {"model": body.get("model") or DEFAULT_MODEL}
    messages = []

    system = body.get("system")
    if system:
        text = _extract_system_blocks(system)
        if text:
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
                continue
            texts = []
            tool_results = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t in ("text", "thinking", "redacted_thinking"):
                    txt = block.get("text")
                    if txt:
                        texts.append(str(txt))
                elif t == "tool_result":
                    tool_results.append(block)
                elif t == "image":
                    img = _image_block_to_openai(block)
                    if img:
                        texts.append(img)
            if texts:
                if all(isinstance(x, str) for x in texts):
                    messages.append({"role": "user", "content": "\n\n".join(texts)})
                else:
                    messages.append({"role": "user", "content": texts})
            for tr in tool_results:
                c = tr.get("content")
                if isinstance(c, list):
                    c = "\n\n".join(
                        b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if tr.get("is_error") and c is not None:
                    c = "[Error] " + str(c)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id", ""),
                    "content": str(c) if c is not None else "",
                })
        elif role == "assistant":
            text_parts = []
            tool_calls = []
            if isinstance(content, str):
                text_parts.append(content)
            else:
                for block in content or []:
                    if not isinstance(block, dict):
                        continue
                    t = block.get("type")
                    if t == "text" and block.get("text"):
                        text_parts.append(str(block["text"]))
                    elif t == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", _uuid("toolu_")),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                            },
                        })
            _append_assistant(messages, "\n\n".join(text_parts), tool_calls or None)

    out["messages"] = messages

    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {},
            },
        })
    if tools:
        out["tools"] = tools

    if body.get("tool_choice") is not None:
        out["tool_choice"] = _map_tool_choice(body["tool_choice"])

    for k in ("max_tokens", "temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    stops = body.get("stop_sequences")
    if stops:
        out["stop"] = [str(s) for s in stops[:4]]

    stream = bool(body.get("stream"))
    out["stream"] = stream
    if stream and USE_STREAM_OPTIONS:
        out["stream_options"] = {"include_usage": True}
    return out


def chat_to_anthropic(data, requested_model):
    """把 OpenAI chat.completion (非流式) 转回 Anthropic message。"""
    choice = (data.get("choices") or [{}])[0]
    m = choice.get("message") or {}
    content = []
    reasoning = m.get("reasoning_content") or m.get("reasoning")
    if reasoning:
        content.append({
            "type": "thinking",
            "thinking": reasoning,
            "signature": "",
        })
    text = m.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", _uuid("toolu_")),
            "name": fn.get("name", ""),
            "input": args,
        })
    if not content:
        content = [{"type": "text", "text": ""}]
    usage = data.get("usage") or {}
    stop_reason = {
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }.get(choice.get("finish_reason"), "end_turn")
    return {
        "id": _uuid("msg_"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def anthropic_sse_translator(upstream, requested_model):
    """把上游 chat.completions 的 SSE 增量翻译成 Anthropic 的 SSE 事件。"""
    msg_id = _uuid("msg_")
    thinking_open = False
    thinking_block_index = None
    text_open = False
    text_block_index = None
    tools = {}
    next_block_index = 0
    stop_reason = None
    output_tokens = 0

    def ev(event, data):
        return "event: %s\ndata: %s\n\n" % (
            event, json.dumps(data, ensure_ascii=False))

    yield ev("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    for line in iter_sse_lines(upstream):
        if b"<html" in line.lower():
            raise RuntimeError("upstream returned an HTML error page instead of SSE")
        chunk = parse_sse_json(line)
        if not chunk:
            continue
        if chunk.get("usage"):
            output_tokens = chunk["usage"].get("completion_tokens", output_tokens)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            fr = choice.get("finish_reason")

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                if not thinking_open:
                    thinking_open = True
                    thinking_block_index = next_block_index
                    next_block_index += 1
                    yield ev("content_block_start", {
                        "type": "content_block_start",
                        "index": thinking_block_index,
                        "content_block": {
                            "type": "thinking", "thinking": "", "signature": ""},
                    })
                yield ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": thinking_block_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                })

            text = delta.get("content")
            if text:
                if not text_open:
                    text_block_index = next_block_index
                    next_block_index += 1
                    yield ev("content_block_start", {
                        "type": "content_block_start",
                        "index": text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    })
                    text_open = True
                yield ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_block_index,
                    "delta": {"type": "text_delta", "text": text},
                })

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                st = tools.setdefault(idx, {
                    "block_index": None, "id": None, "name": None})
                if tc.get("id"):
                    st["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    st["name"] = fn["name"]
                args = fn.get("arguments") or ""
                if st["block_index"] is None and st["id"] and st["name"]:
                    st["block_index"] = next_block_index
                    next_block_index += 1
                    yield ev("content_block_start", {
                        "type": "content_block_start",
                        "index": st["block_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": st["id"],
                            "name": st["name"],
                            "input": {},
                        },
                    })
                if args and st["block_index"] is not None:
                    yield ev("content_block_delta", {
                        "type": "content_block_delta",
                        "index": st["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    })

            if fr == "tool_calls":
                stop_reason = "tool_use"
            elif fr == "stop" and stop_reason is None:
                stop_reason = "end_turn"
            elif fr == "length" and stop_reason is None:
                stop_reason = "max_tokens"

    stop_indexes = []
    if thinking_open:
        stop_indexes.append(thinking_block_index)
    if text_open:
        stop_indexes.append(text_block_index)
    for st in tools.values():
        if st["block_index"] is not None:
            stop_indexes.append(st["block_index"])
    for idx in sorted(stop_indexes):
        yield ev("content_block_stop", {
            "type": "content_block_stop", "index": idx})

    started_tool_block = any(
        st["block_index"] is not None for st in tools.values())
    if started_tool_block:
        stop_reason = "tool_use"
    elif stop_reason is None or stop_reason == "tool_use":
        # 上游 finish_reason 说 tool_calls, 但没发出可用的 tool block -> 回退 end_turn
        stop_reason = "end_turn"

    yield ev("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield ev("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# OpenAI Responses -> OpenAI Chat Completions
# ---------------------------------------------------------------------------

def responses_to_chat(body):
    """把 Codex 的 /v1/responses 请求翻译成 OpenAI chat.completions。"""
    out = {"model": body.get("model") or DEFAULT_MODEL}
    messages = []

    instructions = body.get("instructions")
    if instructions:
        if isinstance(instructions, list):
            instructions = "\n\n".join(str(x) for x in instructions)
        messages.append({"role": "system", "content": str(instructions)})

    inp = body.get("input")
    if inp is None:
        inp = []
    if isinstance(inp, str):
        inp = [{"role": "user", "content": inp}]

    for item in inp:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        role = item.get("role")
        content = item.get("content")

        if typ in (None, "message") and role in ("user", "assistant", "system"):
            text_parts = []
            images = []
            if isinstance(content, str):
                text_parts.append(content)
            else:
                for part in content or []:
                    if isinstance(part, str):
                        text_parts.append(part)
                        continue
                    pt = part.get("type")
                    if pt in ("input_text", "output_text", "text"):
                        txt = part.get("text")
                        if txt:
                            text_parts.append(str(txt))
                    elif pt == "input_image":
                        img = part.get("image_url") or {}
                        url = img.get("url") if isinstance(img, dict) else img
                        if not url:
                            url = part.get("image_url")
                        if url:
                            images.append({
                                "type": "image_url", "image_url": {"url": url}})
            seg = []
            if text_parts:
                seg.append("\n\n".join(text_parts))
            seg.extend(images)
            if not seg:
                continue
            if role == "user" and len(seg) == 1 and isinstance(seg[0], str):
                messages.append({"role": "user", "content": seg[0]})
            else:
                messages.append({"role": role, "content": seg})

        elif typ == "function_call":
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id", _uuid("call_")),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": arguments,
                    },
                }],
            })

        elif typ == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": str(item.get("output", "")),
            })

    out["messages"] = messages

    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {},
            },
        })
    if tools:
        out["tools"] = tools

    tc = body.get("tool_choice")
    if isinstance(tc, str) and tc in ("auto", "required", "none"):
        out["tool_choice"] = tc
    elif isinstance(tc, dict) and tc.get("type") == "function" and tc.get("name"):
        out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}

    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]

    stream = bool(body.get("stream"))
    out["stream"] = stream
    if stream and USE_STREAM_OPTIONS:
        out["stream_options"] = {"include_usage": True}
    return out


def chat_to_responses(data, requested_model):
    """把 OpenAI chat.completion (非流式) 转回 Responses 对象。"""
    choice = (data.get("choices") or [{}])[0]
    m = choice.get("message") or {}
    incomplete = choice.get("finish_reason") == "length"
    output = []
    reasoning = m.get("reasoning_content") or m.get("reasoning")
    if reasoning:
        output.append({
            "id": _uuid("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    text = m.get("content")
    if text:
        output.append({
            "id": _uuid("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "id": _uuid("fc_"),
            "type": "function_call",
            "status": "completed",
            "call_id": tc.get("id", _uuid("call_")),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
        })
    usage = data.get("usage") or {}
    return {
        "id": _uuid("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": requested_model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "error": None,
    }


def responses_sse_translator(upstream, requested_model):
    """把上游 chat.completions 的 SSE 增量翻译成 Responses 的 SSE 事件。"""
    resp_id = _uuid("resp_")
    created_at = int(time.time())
    reasoning_open = False
    reasoning_index = None
    reasoning_item_id = None
    reasoning_buf = []
    msg_item_id = _uuid("msg_")
    text_buf = []
    text_open = False
    text_index = None
    next_output_index = 0
    fc_states = {}
    usage = {}
    stop_reason = None

    def ev(event, data):
        return "event: %s\ndata: %s\n\n" % (
            event, json.dumps(data, ensure_ascii=False))

    yield ev("response.created", {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "in_progress",
            "model": requested_model,
            "output": [],
            "usage": None,
        },
    })
    yield ev("response.in_progress", {
        "type": "response.in_progress",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "in_progress",
            "model": requested_model,
            "output": [],
            "usage": None,
        },
    })

    for line in iter_sse_lines(upstream):
        if b"<html" in line.lower():
            raise RuntimeError("upstream returned an HTML error page instead of SSE")
        chunk = parse_sse_json(line)
        if not chunk:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            fr = choice.get("finish_reason")

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                if not reasoning_open:
                    reasoning_open = True
                    reasoning_item_id = _uuid("rs_")
                    reasoning_index = next_output_index
                    next_output_index += 1
                    yield ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": reasoning_index,
                        "item": {
                            "id": reasoning_item_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "summary": [],
                        },
                    })
                reasoning_buf.append(reasoning)
                yield ev("response.reasoning_summary_text.delta", {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_index,
                    "summary_text": reasoning,
                })

            text = delta.get("content")
            if text:
                if not text_open:
                    text_open = True
                    text_index = next_output_index
                    next_output_index += 1
                    yield ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": text_index,
                        "item": {
                            "id": msg_item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    })
                    yield ev("response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": msg_item_id,
                        "output_index": text_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                text_buf.append(text)
                yield ev("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": msg_item_id,
                    "output_index": text_index,
                    "content_index": 0,
                    "delta": text,
                })

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                st = fc_states.setdefault(idx, {
                    "id": None, "name": None, "args": [],
                    "item_id": None, "output_index": None})
                if tc.get("id"):
                    st["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    st["name"] = fn["name"]
                args = fn.get("arguments") or ""
                if args:
                    st["args"].append(args)
                if st["item_id"] is None and st["id"] and st["name"]:
                    st["item_id"] = _uuid("fc_")
                    st["output_index"] = next_output_index
                    next_output_index += 1
                    yield ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": st["output_index"],
                        "item": {
                            "id": st["item_id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": st["id"],
                            "name": st["name"],
                            "arguments": "",
                        },
                    })
                if args and st["item_id"] is not None:
                    yield ev("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": st["item_id"],
                        "output_index": st["output_index"],
                        "delta": args,
                    })

            if fr == "tool_calls":
                stop_reason = "tool_use"
            elif fr == "stop" and stop_reason is None:
                stop_reason = "end_turn"
            elif fr == "length" and stop_reason is None:
                stop_reason = "max_tokens"

    output = []
    if reasoning_open:
        full_reasoning = "".join(reasoning_buf)
        reasoning_item = {
            "id": reasoning_item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": full_reasoning}],
        }
        yield ev("response.reasoning_summary_text.done", {
            "type": "response.reasoning_summary_text.done",
            "item_id": reasoning_item_id,
            "output_index": reasoning_index,
            "summary_text": full_reasoning,
        })
        yield ev("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": reasoning_index,
            "item": reasoning_item,
        })
        output.append(reasoning_item)

    if text_open:
        full_text = "".join(text_buf)
        msg_item = {
            "id": msg_item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text, "annotations": []}],
        }
        yield ev("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_item_id,
            "output_index": text_index,
            "content_index": 0,
            "text": full_text,
        })
        yield ev("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_item_id,
            "output_index": text_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []},
        })
        yield ev("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": text_index,
            "item": msg_item,
        })
        output.append(msg_item)

    for st in fc_states.values():
        if st["item_id"] is None:
            continue
        full_args = "".join(st["args"])
        item = {
            "id": st["item_id"],
            "type": "function_call",
            "status": "completed",
            "call_id": st["id"],
            "name": st["name"],
            "arguments": full_args,
        }
        yield ev("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "item_id": st["item_id"],
            "output_index": st["output_index"],
            "arguments": full_args,
        })
        yield ev("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": st["output_index"],
            "item": item,
        })
        output.append(item)

    incomplete = stop_reason == "max_tokens"
    yield ev("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "incomplete" if incomplete else "completed",
            "model": requested_model,
            "output": output,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        },
    })


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenCodeGoRelay/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    # -- 基础工具 ----------------------------------------------------------

    def _check_auth(self):
        if not RELAY_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            if hmac.compare_digest(auth[7:].strip(), RELAY_TOKEN):
                return True
        if hmac.compare_digest(self.headers.get("x-api-key", "").strip(), RELAY_TOKEN):
            return True
        return False

    def _get_api_key(self):
        """从请求头提取客户端携带的上游 key (Claude Code 的 ANTHROPIC_AUTH_TOKEN 即走这里)。

        排除与 RELAY_TOKEN 相同的值, 避免把 relay 门禁 token 误当上游 key 转发。
        """
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
            if key and (not RELAY_TOKEN or not hmac.compare_digest(key, RELAY_TOKEN)):
                return key
        xkey = self.headers.get("x-api-key", "").strip()
        if xkey and (not RELAY_TOKEN or not hmac.compare_digest(xkey, RELAY_TOKEN)):
            return xkey
        return ""

    def _upstream_api_key(self):
        """本次请求使用的上游 key。设置过 GO_KEY 就统一用它 (服务器级模式);
        否则透传请求头里的 key (按请求取 key 模式)。"""
        if GO_KEY:
            return GO_KEY
        return self._get_api_key()

    def _send_json(self, status, obj):
        data = json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _send_sse_error(self, message, anthropic_style):
        """SSE 头已发出后再出错时, 在流内写 error 事件而不是回写 HTTP 状态行。"""
        if anthropic_style:
            data = {"type": "error", "error": {"type": "api_error", "message": message}}
        else:
            data = {"type": "error", "code": "api_error", "message": message}
        try:
            self.wfile.write(b"event: error\ndata: " + json_bytes(data) + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").strip().lower() not in ("", "identity"):
            self._send_json(400, {"error": {"message": "Transfer-Encoding not supported"}})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if length < 0:
            self._send_json(400, {"error": {"message": "invalid Content-Length"}})
            return None
        if length == 0:
            self._raw_body = b"{}"
            return {}
        if length > MAX_BODY:
            self._send_json(413, {"error": {"message": "request body too large"}})
            return None
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, ConnectionResetError):
            self._send_json(408, {"error": {"message": "request body read timed out"}})
            return None
        try:
            self._raw_body = raw
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": {"message": "invalid JSON body"}})
            return None

    def _write_chunks(self, chunks):
        try:
            for c in chunks:
                self.wfile.write(c.encode("utf-8") if isinstance(c, str) else c)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _upstream_error(self, exc, anthropic_style):
        status = exc.code
        raw = ""
        try:
            raw = exc.read(65536).decode("utf-8", "replace")
        except Exception:
            pass
        if raw and (raw.lstrip().startswith("<") or "<html" in raw[:1024].lower()):
            raw = ""
        if anthropic_style:
            self._send_json(status, {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": raw or "upstream request failed",
                },
            })
        else:
            self._send_json(status, {
                "error": {"type": "api_error", "message": raw or "upstream request failed"},
            })

    # -- 路由 --------------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._check_auth():
            self._send_json(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "invalid relay token"}})
            return
        body = self._read_body()
        if body is None:
            return
        api_key = self._upstream_api_key()
        session = self.headers.get("x-opencode-session", "").strip()
        if not api_key:
            self._send_json(401, {
                "error": {"message": "missing API key (send Authorization: Bearer <key> or x-api-key: <key>)"},
            })
            return
        if path == "/v1/messages":
            if ANTHROPIC_PASSTHROUGH:
                self._handle_anthropic_passthrough(getattr(self, "_raw_body", b""), api_key, session)
            else:
                self._handle_anthropic(body, api_key, session)
        elif path == "/v1/messages/count_tokens":
            self._handle_count_tokens(body)
        elif path == "/v1/responses":
            self._handle_responses(body, api_key, session)
        elif path == "/v1/chat/completions":
            self._handle_chat_passthrough(body, api_key, session)
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path != "/healthz" and not self._check_auth():
            self._send_json(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "invalid relay token"}})
            return
        if path == "/healthz":
            self._send_json(200, {"ok": True})
        elif path == "/v1/models":
            models = [{"id": DEFAULT_MODEL, "object": "model", "created": 0, "owned_by": "opencode-go"}]
            for extra in MODELS_EXTRA:
                models.append({"id": extra, "object": "model", "created": 0, "owned_by": "opencode-go"})
            self._send_json(200, {"object": "list", "data": models})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    # -- Anthropic 端点 ----------------------------------------------------

    def _handle_anthropic_passthrough(self, raw_body, api_key, session=None):
        """薄透传:原始 Anthropic body → 上游 /messages,响应字节原样回给客户端。

        收益(2026-10-03 实测对比翻译模式):prompt caching 的 cache_read_input_tokens /
        cache_creation_input_tokens 原生回传;thinking 块、tool_use、SSE 事件序列、
        anthropic-beta 语义全部保持上游原生,不再经过 OpenAI 中间格式。
        """
        hdr = {}
        for name in ("anthropic-version", "anthropic-beta"):
            vals = self.headers.get_all(name) or []
            if vals:
                hdr[name] = ", ".join(vals)
        hdr.setdefault("anthropic-version", "2023-06-01")
        # 会话亲和:优先用 Claude Code 的会话 id → 客户端给的 x-opencode-session → 合成一个。
        # (上游 Anthropic 端点并不强制这个头;这里转发只为后端亲和与可观测。)
        sid = (self.headers.get("X-Claude-Code-Session-Id", "") or "").strip() \
            or (session or "").strip() or _uuid("ses_")
        hdr["x-opencode-session"] = sid
        is_stream = False
        try:
            is_stream = json.loads(raw_body.decode("utf-8")).get("stream") is True
        except Exception:
            pass
        try:
            upstream = upstream_anthropic_request("/messages", raw_body, headers=hdr, api_key=api_key)
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(65536)
            except Exception:
                pass
            if not raw:
                raw = json_bytes({"type": "error", "error": {
                    "type": "api_error", "message": "upstream request failed"}})
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        except Exception as exc:
            self._send_json(502, {"type": "error", "error": {
                "type": "api_error", "message": "upstream unreachable: %s" % exc}})
            return
        try:
            if is_stream:
                self._start_sse()
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                data = upstream.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _handle_count_tokens(self, body):
        """上游没有 count_tokens 端点(实测 404)→ 本地估算。

        用 tiktoken o200k_base 近似 DeepSeek 分词器,量级正确但非权威计数(见 estimate_input_tokens)。
        """
        try:
            n = estimate_input_tokens(body or {})
        except Exception:
            n = 1
        self._send_json(200, {"input_tokens": n})

    def _handle_anthropic(self, body, api_key, session=None):
        requested_model = body.get("model") or DEFAULT_MODEL
        chat = anthropic_to_openai(body)
        hdr = {"x-opencode-session": session} if session else None
        try:
            upstream = upstream_request("/chat/completions", chat, api_key=api_key, headers=hdr)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 422) and USE_STREAM_OPTIONS and chat.get("stream"):
                # 某些上游不认 stream_options, 去掉重试一次
                chat.pop("stream_options", None)
                try:
                    upstream = upstream_request("/chat/completions", chat, api_key=api_key, headers=hdr)
                except urllib.error.HTTPError as exc2:
                    self._upstream_error(exc2, anthropic_style=True)
                    return
            else:
                self._upstream_error(exc, anthropic_style=True)
                return
        except Exception as exc:
            self._send_json(502, {
                "type": "error",
                "error": {"type": "api_error", "message": str(exc)},
            })
            return

        try:
            if not chat["stream"]:
                try:
                    data = json.loads(upstream.read().decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(502, {
                        "type": "error",
                        "error": {"type": "api_error",
                                  "message": "upstream returned non-JSON response"},
                    })
                    return
                self._send_json(200, chat_to_anthropic(data, requested_model))
            else:
                self._start_sse()
                try:
                    self._write_chunks(anthropic_sse_translator(upstream, requested_model))
                except Exception as exc:
                    self._send_sse_error(str(exc), anthropic_style=True)
        finally:
            upstream.close()

    # -- Responses 端点 ----------------------------------------------------

    def _handle_responses(self, body, api_key, session=None):
        requested_model = body.get("model") or DEFAULT_MODEL
        chat = responses_to_chat(body)
        hdr = {"x-opencode-session": session} if session else None
        try:
            upstream = upstream_request("/chat/completions", chat, api_key=api_key, headers=hdr)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 422) and USE_STREAM_OPTIONS and chat.get("stream"):
                chat.pop("stream_options", None)
                try:
                    upstream = upstream_request("/chat/completions", chat, api_key=api_key, headers=hdr)
                except urllib.error.HTTPError as exc2:
                    self._upstream_error(exc2, anthropic_style=False)
                    return
            else:
                self._upstream_error(exc, anthropic_style=False)
                return
        except Exception as exc:
            self._send_json(502, {"error": {"message": str(exc)}})
            return

        try:
            if not chat["stream"]:
                try:
                    data = json.loads(upstream.read().decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(502, {
                        "error": {"message": "upstream returned non-JSON response"},
                    })
                    return
                self._send_json(200, chat_to_responses(data, requested_model))
            else:
                self._start_sse()
                try:
                    self._write_chunks(responses_sse_translator(upstream, requested_model))
                except Exception as exc:
                    self._send_sse_error(str(exc), anthropic_style=False)
        finally:
            upstream.close()

    # -- OpenAI chat 直通 ---------------------------------------------------

    def _handle_chat_passthrough(self, body, api_key, session=None):
        body = dict(body)
        body["model"] = body.get("model") or DEFAULT_MODEL
        hdr = {"x-opencode-session": session} if session else None
        try:
            upstream = upstream_request("/chat/completions", body, api_key=api_key, headers=hdr)
        except urllib.error.HTTPError as exc:
            self._upstream_error(exc, anthropic_style=False)
            return
        except Exception as exc:
            self._send_json(502, {"error": {"message": str(exc)}})
            return

        ctype = upstream.headers.get("Content-Type", "application/json")
        self.send_response(upstream.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """每连接一线程, 但用信号量限制并发, 防公网连接洪水。"""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._conn_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        self._conn_slots.acquire()
        try:
            request.settimeout(CLIENT_TIMEOUT)
            super().process_request(request, client_address)
        except Exception:
            self._conn_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._conn_slots.release()


def main():
    if not GO_KEY:
        sys.stderr.write(
            "WARNING: OPENCODE_GO_API_KEY is empty; each request must carry its own "
            "API key (Authorization: Bearer <key> or x-api-key: <key>).\n")
    if not RELAY_TOKEN:
        sys.stderr.write(
            "WARNING: RELAY_TOKEN is empty; anyone can use this relay. "
            "Set RELAY_TOKEN or restrict access (e.g. IP allowlist) before "
            "exposing it publicly.\n")
    server = LimitedThreadingHTTPServer((HOST, PORT), RelayHandler)

    def _shutdown(signum, frame):
        sys.stderr.write("signal %d received, shutting down\n" % signum)
        # shutdown() 会阻塞等待 serve_forever 退出, 不能在主线程的信号处理器里直接调
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    sys.stderr.write(
        "OpenCode Go relay listening on http://%s:%d -> %s (model: %s, /v1/messages: %s)\n"
        % (HOST, PORT, UPSTREAM_BASE, DEFAULT_MODEL,
           "anthropic-passthrough" if ANTHROPIC_PASSTHROUGH else "translate-to-openai"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
