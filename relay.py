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
  OPENCODE_GO_API_KEY   必填, 你的 OpenCode Go API key (只放在服务器上)
  RELAY_TOKEN           可选, 强烈建议公网部署时设置, 客户端用它鉴权
  DEFAULT_MODEL         默认 deepseek-v4-flash
  UPSTREAM_BASE         默认 https://opencode.ai/zen/go/v1
  HOST / PORT           默认 0.0.0.0:8787
  STREAM_OPTIONS        默认 1 (向上游请求 usage), 上游报错可设 0

仅用 Python 标准库, 无第三方依赖。Python 3.9+。
"""

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://opencode.ai/zen/go/v1").rstrip("/")
GO_KEY = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "deepseek-v4-flash").strip()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "600"))
USE_STREAM_OPTIONS = os.environ.get("STREAM_OPTIONS", "1") != "0"
MAX_BODY = 64 * 1024 * 1024  # 64 MB

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

def upstream_request(path, payload, headers=None):
    """POST 到 OpenCode Go 上游, 返回 http.client.HTTPResponse (可流式读)。"""
    req = urllib.request.Request(
        UPSTREAM_BASE + path,
        data=json_bytes(payload),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + GO_KEY)
    req.add_header("User-Agent", UPSTREAM_UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


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
        msg["content"] = msg.get("content")
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
    out = {"model": DEFAULT_MODEL}
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
    usage = data.get("usage") or {}
    return {
        "id": _uuid("msg_"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": "tool_use" if choice.get("finish_reason") == "tool_calls" else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def anthropic_sse_translator(upstream, requested_model):
    """把上游 chat.completions 的 SSE 增量翻译成 Anthropic 的 SSE 事件。"""
    msg_id = _uuid("msg_")
    text_open = False
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
        chunk = parse_sse_json(line)
        if not chunk:
            continue
        if chunk.get("usage"):
            output_tokens = chunk["usage"].get("completion_tokens", output_tokens)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            fr = choice.get("finish_reason")

            text = delta.get("content")
            if text:
                if not text_open:
                    yield ev("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })
                    text_open = True
                    next_block_index = 1
                yield ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
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

    if text_open:
        yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
    for st in tools.values():
        if st["block_index"] is not None:
            yield ev("content_block_stop", {
                "type": "content_block_stop", "index": st["block_index"]})

    if stop_reason is None:
        stop_reason = "end_turn"
    if tools and stop_reason == "end_turn":
        stop_reason = "tool_use"

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
    out = {"model": DEFAULT_MODEL}
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
    output = []
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
        "status": "completed",
        "model": requested_model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
    }


def responses_sse_translator(upstream, requested_model):
    """把上游 chat.completions 的 SSE 增量翻译成 Responses 的 SSE 事件。"""
    resp_id = _uuid("resp_")
    created_at = int(time.time())
    msg_item_id = _uuid("msg_")
    text_buf = []
    text_open = False
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

    for line in iter_sse_lines(upstream):
        chunk = parse_sse_json(line)
        if not chunk:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            fr = choice.get("finish_reason")

            text = delta.get("content")
            if text:
                if not text_open:
                    text_open = True
                    yield ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": 0,
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
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    })
                text_buf.append(text)
                yield ev("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": msg_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                })

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                st = fc_states.setdefault(idx, {
                    "id": None, "name": None, "args": [], "item_id": None})
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
                    yield ev("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": 0,
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
                        "output_index": 0,
                        "delta": args,
                    })

            if fr == "tool_calls":
                stop_reason = "tool_use"
            elif fr == "stop" and stop_reason is None:
                stop_reason = "end_turn"

    output = []
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
            "output_index": 0,
            "content_index": 0,
            "text": full_text,
        })
        yield ev("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []},
        })
        yield ev("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
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
            "output_index": 0,
            "arguments": full_args,
        })
        yield ev("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": item,
        })
        output.append(item)

    yield ev("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": requested_model,
            "output": output,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
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
        if auth.startswith("Bearer ") and auth[7:].strip() == RELAY_TOKEN:
            return True
        if self.headers.get("x-api-key", "").strip() == RELAY_TOKEN:
            return True
        return False

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

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            self._send_json(413, {"error": {"message": "request body too large"}})
            return None
        raw = self.rfile.read(length)
        try:
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
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
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
            self._send_json(401, {"error": {"message": "invalid relay token"}})
            return
        body = self._read_body()
        if body is None:
            return
        if path == "/v1/messages":
            self._handle_anthropic(body)
        elif path == "/v1/responses":
            self._handle_responses(body)
        elif path == "/v1/chat/completions":
            self._handle_chat_passthrough(body)
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path != "/healthz" and not self._check_auth():
            self._send_json(401, {"error": {"message": "invalid relay token"}})
            return
        if path == "/healthz":
            self._send_json(200, {"ok": True})
        elif path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": DEFAULT_MODEL,
                    "object": "model",
                    "created": 0,
                    "owned_by": "opencode-go",
                }],
            })
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    # -- Anthropic 端点 ----------------------------------------------------

    def _handle_anthropic(self, body):
        requested_model = body.get("model") or DEFAULT_MODEL
        chat = anthropic_to_openai(body)
        try:
            upstream = upstream_request("/chat/completions", chat)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 422) and USE_STREAM_OPTIONS and chat.get("stream"):
                # 某些上游不认 stream_options, 去掉重试一次
                chat.pop("stream_options", None)
                try:
                    upstream = upstream_request("/chat/completions", chat)
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
                data = json.loads(upstream.read().decode("utf-8"))
                self._send_json(200, chat_to_anthropic(data, requested_model))
            else:
                self._start_sse()
                self._write_chunks(anthropic_sse_translator(upstream, requested_model))
        except Exception as exc:
            if not self.wfile.closed:
                self._send_json(502, {
                    "type": "error",
                    "error": {"type": "api_error", "message": str(exc)},
                })
        finally:
            upstream.close()

    # -- Responses 端点 ----------------------------------------------------

    def _handle_responses(self, body):
        requested_model = body.get("model") or DEFAULT_MODEL
        chat = responses_to_chat(body)
        try:
            upstream = upstream_request("/chat/completions", chat)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 422) and USE_STREAM_OPTIONS and chat.get("stream"):
                chat.pop("stream_options", None)
                try:
                    upstream = upstream_request("/chat/completions", chat)
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
                data = json.loads(upstream.read().decode("utf-8"))
                self._send_json(200, chat_to_responses(data, requested_model))
            else:
                self._start_sse()
                self._write_chunks(responses_sse_translator(upstream, requested_model))
        except Exception as exc:
            if not self.wfile.closed:
                self._send_json(502, {"error": {"message": str(exc)}})
        finally:
            upstream.close()

    # -- OpenAI chat 直通 ---------------------------------------------------

    def _handle_chat_passthrough(self, body):
        body = dict(body)
        body["model"] = DEFAULT_MODEL
        try:
            upstream = upstream_request("/chat/completions", body)
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


def main():
    if not GO_KEY:
        sys.stderr.write(
            "WARNING: OPENCODE_GO_API_KEY is empty; upstream requests will fail.\n")
    if not RELAY_TOKEN:
        sys.stderr.write(
            "WARNING: RELAY_TOKEN is empty; anyone can use this relay. "
            "Set RELAY_TOKEN before exposing it publicly.\n")
    server = ThreadingHTTPServer((HOST, PORT), RelayHandler)
    server.daemon_threads = True
    sys.stderr.write(
        "OpenCode Go relay listening on http://%s:%d -> %s (model: %s)\n"
        % (HOST, PORT, UPSTREAM_BASE, DEFAULT_MODEL))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
