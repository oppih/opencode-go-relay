#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地集成测试: 用 mock 上游模拟 OpenCode Go 的 /v1/chat/completions,
验证 relay 的 Anthropic / Responses / chat 直通三条路径。

运行: python3 test_relay.py
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RELAY_PY = os.path.join(SCRIPT_DIR, "relay.py")


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    last_chat_request = None
    chat_requests = []
    last_auth = None

    def log_message(self, *args):
        pass

    def _send(self, status, ctype, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw.decode("utf-8"))
        MockUpstream.last_chat_request = payload
        MockUpstream.chat_requests.append(payload)
        MockUpstream.last_auth = self.headers.get("Authorization", "")
        user_text = "\n".join(
            str(m.get("content", ""))
            for m in payload.get("messages") or []
            if isinstance(m.get("content"), str)
        )

        if "TRIGGER_HTML_ERROR" in user_text:
            self._send(502, "text/html", b"<html><body>cloudflare blocked</body></html>")
            return

        if "TRIGGER_NONJSON_200" in user_text:
            self._send(200, "text/html", b"<html>gateway hiccup</html>")
            return

        if not payload.get("stream"):
            if "TRIGGER_LENGTH" in user_text:
                body = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-v4-flash",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "cut"},
                        "finish_reason": "length",
                    }],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                              "total_tokens": 5},
                }
                self._send(200, "application/json", json.dumps(body).encode("utf-8"))
                return
            if "TRIGGER_EMPTY_CONTENT" in user_text:
                body = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-v4-flash",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": None},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                              "total_tokens": 4},
                }
                self._send(200, "application/json", json.dumps(body).encode("utf-8"))
                return
            body = {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-flash",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "Bash",
                                "arguments": '{"command": "ls"}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 5,
                    "total_tokens": 16,
                },
            }
            self._send(200, "application/json", json.dumps(body).encode("utf-8"))
            return

        if "TRIGGER_RETRY" in user_text and payload.get("stream_options"):
            err = {"error": {"message": "stream_options unsupported"}}
            self._send(400, "application/json", json.dumps(err).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        if "TRIGGER_HTML_SSE" in user_text:
            self.wfile.write(b"data: <html>blocked</html>\n\n")
            self.wfile.flush()
            return
        if "TRIGGER_LENGTH_STREAM" in user_text:
            chunks = [
                {"choices": [{"index": 0, "delta": {"content": "cut"},
                              "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                 "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                           "total_tokens": 5}},
            ]
        elif "TRIGGER_SSE_ERROR" in user_text:
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-v4-flash",
                 "choices": [{"index": 0, "delta": {"content": "Hello"},
                              "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": "oops"}]},
            ]
        elif "TRIGGER_MULTI_TOOL" in user_text:
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-v4-flash",
                 "choices": [{"index": 0, "delta": {"content": "Hello"},
                              "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1",
                     "function": {"name": "Bash", "arguments": '{"command": "ls"}'}},
                    {"index": 1, "id": "call_2",
                     "function": {"name": "Read", "arguments": '{"path": "/tmp"}'}},
                ]}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 5,
                           "total_tokens": 16}},
            ]
        elif "TRIGGER_TOOL_FIRST" in user_text:
            chunks = [
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1",
                     "function": {"name": "Bash", "arguments": '{"a":1}'}}]},
                    "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "done"},
                              "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
        elif "TRIGGER_MISSING_TOOL_NAME" in user_text:
            chunks = [
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"x":1}'}}]},
                    "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1,
                 "model": "deepseek-v4-flash",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"},
                              "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_1",
                    "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}]},
                    "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 5,
                           "total_tokens": 16}},
            ]
        for c in chunks:
            self.wfile.write(b"data: " + json.dumps(c).encode("utf-8") + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def start_mock():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def wait_ready(port, timeout=15):
    url = "http://127.0.0.1:%d/healthz" % port
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def post(url, payload, token="secret", timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")


def parse_sse_events(text):
    events = []
    current = None
    data_lines = []
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            if current is not None:
                events.append((current, json.loads("\n".join(data_lines))))
            current = None
            data_lines = []
    return events


ANTHROPIC_REQUEST = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 512,
    "stream": False,
    "system": [{"type": "text", "text": "You are a helpful assistant."}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "List files"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_01", "name": "Bash",
             "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "toolu_01",
            "content": [{"type": "text", "text": "file1\nfile2"}],
        }]},
    ],
    "tools": [{
        "name": "Bash", "description": "Run a command",
        "input_schema": {"type": "object",
                         "properties": {"command": {"type": "string"}},
                         "required": ["command"]},
    }],
    "tool_choice": {"type": "auto"},
}


RESPONSES_REQUEST = {
    "model": "gpt-5.2-codex",
    "stream": False,
    "instructions": "You are Codex.",
    "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "Run ls"}]},
        {"type": "function_call", "call_id": "call_99", "name": "Bash",
         "arguments": '{"command":"ls"}'},
        {"type": "function_call_output", "call_id": "call_99", "output": "file1"},
    ],
    "tools": [{
        "type": "function", "name": "Bash", "description": "Run",
        "parameters": {"type": "object"},
    }],
    "tool_choice": "auto",
}


def test_anthropic_nonstream(base):
    status, text, _ = post(base + "/v1/messages", ANTHROPIC_REQUEST)
    assert status == 200, (status, text)
    msg = json.loads(text)
    assert msg["type"] == "message"
    assert msg["stop_reason"] == "tool_use"
    types = [c["type"] for c in msg["content"]]
    assert types == ["text", "tool_use"], types
    tool = msg["content"][1]
    assert tool["name"] == "Bash" and tool["input"] == {"command": "ls"}
    assert msg["usage"]["input_tokens"] == 11

    up = MockUpstream.last_chat_request
    assert up["model"] == "deepseek-v4-flash"
    assert up["stream"] is False
    roles = [m["role"] for m in up["messages"]]
    assert roles == ["system", "user", "assistant", "tool"], roles
    assert up["messages"][2]["tool_calls"][0]["id"] == "toolu_01"
    assert up["messages"][3]["tool_call_id"] == "toolu_01"
    assert up["messages"][3]["content"] == "file1\nfile2"
    assert up["tools"][0]["function"]["name"] == "Bash"
    assert up["tool_choice"] == "auto"
    print("PASS anthropic non-stream")


def test_anthropic_stream(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = True
    status, text, ctype = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    assert "text/event-stream" in ctype
    events = parse_sse_events(text)
    names = [e for e, _ in events]
    assert "message_start" in names
    assert "content_block_start" in names
    assert "content_block_delta" in names
    assert "message_delta" in names and "message_stop" in names
    start = [d for e, d in events if e == "message_start"][0]
    assert start["message"]["model"] == "claude-sonnet-4-5"
    md = [d for e, d in events if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use"
    # input_json_delta 不是独立事件名,而是 content_block_delta 数据的 delta.type
    deltas = [d for e, d in events if e == "content_block_delta"]
    assert any(d["delta"].get("type") == "text_delta" and d["delta"]["text"] == "Hello"
               for d in deltas)
    assert any(d["delta"].get("type") == "input_json_delta"
               and "command" in d["delta"].get("partial_json", "")
               for d in deltas)
    print("PASS anthropic stream")


def test_responses_nonstream(base):
    status, text, _ = post(base + "/v1/responses", RESPONSES_REQUEST)
    assert status == 200, (status, text)
    resp = json.loads(text)
    assert resp["object"] == "response" and resp["status"] == "completed"
    types = [o["type"] for o in resp["output"]]
    assert types == ["message", "function_call"], types
    fc = resp["output"][1]
    assert fc["name"] == "Bash" and fc["call_id"] == "call_1"
    assert resp["usage"]["input_tokens"] == 11

    up = MockUpstream.last_chat_request
    assert up["model"] == "deepseek-v4-flash"
    roles = [m["role"] for m in up["messages"]]
    assert roles == ["system", "user", "assistant", "tool"], roles
    assert up["messages"][2]["tool_calls"][0]["id"] == "call_99"
    assert up["messages"][3]["tool_call_id"] == "call_99"
    print("PASS responses non-stream")


def test_responses_stream(base):
    req = dict(RESPONSES_REQUEST)
    req["stream"] = True
    status, text, ctype = post(base + "/v1/responses", req)
    assert status == 200, (status, text)
    assert "text/event-stream" in ctype
    events = parse_sse_events(text)
    names = [e for e, _ in events]
    assert "response.created" in names
    assert "response.in_progress" in names
    assert "response.output_item.added" in names
    assert "response.output_text.delta" in names
    assert "response.function_call_arguments.delta" in names
    assert "response.completed" in names
    completed = [d for e, d in events if e == "response.completed"][0]
    assert completed["response"]["status"] == "completed"
    out_types = [o["type"] for o in completed["response"]["output"]]
    assert "message" in out_types and "function_call" in out_types
    print("PASS responses stream")


def test_chat_passthrough(base):
    status, text, _ = post(base + "/v1/chat/completions", {
        "model": "whatever",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    })
    assert status == 200, (status, text)
    data = json.loads(text)
    assert data["choices"][0]["message"]["content"] == "Hello"
    assert MockUpstream.last_chat_request["model"] == "deepseek-v4-flash"
    print("PASS chat passthrough")


def test_auth(base):
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "should have been rejected"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    print("PASS auth")


def test_models(base):
    req = urllib.request.Request(base + "/v1/models")
    req.add_header("Authorization", "Bearer secret")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data["data"][0]["id"] == "deepseek-v4-flash"
    print("PASS models")


def test_per_request_key(base):
    # 不带 key -> 401
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "missing key should be rejected"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401

    # Authorization: Bearer <key> -> 原样转发上游
    status, text, _ = post(base + "/v1/messages", ANTHROPIC_REQUEST,
                           token="user-key-bearer")
    assert status == 200, (status, text)
    assert MockUpstream.last_auth == "Bearer user-key-bearer"

    # x-api-key: <key> -> 同样转发为 Bearer
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", "user-key-xapi")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
    assert MockUpstream.last_auth == "Bearer user-key-xapi"
    print("PASS per-request key passthrough")


def test_relay_token_not_forwarded(base):
    # 只带 relay token -> 门禁通过但没有上游 key -> 401
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer relay-gate")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "should be 401"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401

    # relay token 在 Bearer, 上游 key 在 x-api-key -> 转发的是 key 不是 token
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer relay-gate")
    req.add_header("x-api-key", "user-key-xapi")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
    assert MockUpstream.last_auth == "Bearer user-key-xapi"

    # 上游 key 在 Bearer, relay token 在 x-api-key -> 同理
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer user-key-bearer2")
    req.add_header("x-api-key", "relay-gate")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
    assert MockUpstream.last_auth == "Bearer user-key-bearer2"

    # 只带上游 key、不带 relay token -> 401
    req = urllib.request.Request(base + "/v1/messages",
                                 data=json.dumps(ANTHROPIC_REQUEST).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer user-key-only")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "should be 401"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    print("PASS relay token not forwarded upstream")


def test_nonjson_upstream(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = False
    req["messages"] = [{"role": "user", "content": "TRIGGER_NONJSON_200"}]
    r = urllib.request.Request(base + "/v1/messages",
                               data=json.dumps(req).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer secret")
    try:
        urllib.request.urlopen(r, timeout=30)
        assert False, "should be 502"
    except urllib.error.HTTPError as exc:
        assert exc.code == 502
        assert "non-JSON" in json.loads(exc.read().decode("utf-8"))["error"]["message"]

    rq = dict(RESPONSES_REQUEST)
    rq["stream"] = False
    rq["input"] = [{"role": "user", "content": "TRIGGER_NONJSON_200"}]
    r = urllib.request.Request(base + "/v1/responses",
                               data=json.dumps(rq).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer secret")
    try:
        urllib.request.urlopen(r, timeout=30)
        assert False, "should be 502"
    except urllib.error.HTTPError as exc:
        assert exc.code == 502
        assert "non-JSON" in json.loads(exc.read().decode("utf-8"))["error"]["message"]
    print("PASS upstream non-JSON 200 -> 502")


def test_length_finish_reason(base):
    # 非流式 anthropic: length -> max_tokens
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = False
    req["messages"] = [{"role": "user", "content": "TRIGGER_LENGTH"}]
    status, text, _ = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    assert json.loads(text)["stop_reason"] == "max_tokens"

    # 非流式 responses: length -> incomplete
    rq = dict(RESPONSES_REQUEST)
    rq["stream"] = False
    rq["input"] = [{"role": "user", "content": "TRIGGER_LENGTH"}]
    status, text, _ = post(base + "/v1/responses", rq)
    assert status == 200, (status, text)
    resp = json.loads(text)
    assert resp["status"] == "incomplete"
    assert resp["incomplete_details"] == {"reason": "max_output_tokens"}

    # 流式 anthropic
    req["stream"] = True
    req["messages"] = [{"role": "user", "content": "TRIGGER_LENGTH_STREAM"}]
    status, text, _ = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    events = parse_sse_events(text)
    md = [d for e, d in events if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "max_tokens", md

    # 流式 responses
    rq["stream"] = True
    rq["input"] = [{"role": "user", "content": "TRIGGER_LENGTH_STREAM"}]
    status, text, _ = post(base + "/v1/responses", rq)
    assert status == 200, (status, text)
    events = parse_sse_events(text)
    completed = [d for e, d in events if e == "response.completed"][0]
    assert completed["response"]["status"] == "incomplete"
    assert completed["response"]["incomplete_details"]["reason"] == "max_output_tokens"
    print("PASS finish_reason length mapping")


def test_html_sse_guard(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = True
    req["messages"] = [{"role": "user", "content": "TRIGGER_HTML_SSE"}]
    status, text, _ = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    events = parse_sse_events(text)
    assert [d for e, d in events if e == "error"], [e for e, _ in events]
    print("PASS html SSE guard")


def test_client_socket_timeout(base, port):
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    # 只发半个请求头, 不发完 -> 服务器应在 CLIENT_TIMEOUT 后断开
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\n")
    time.sleep(3.5)
    s.settimeout(2)
    data = s.recv(4096)
    s.close()
    assert data == b"", data[:100]
    print("PASS client socket timeout (slowloris)")


def test_sigterm_graceful(proc):
    proc.terminate()
    rc = proc.wait(timeout=10)
    assert rc == 0, rc
    print("PASS SIGTERM graceful shutdown")


def test_bad_env_exits_cleanly():
    env = dict(os.environ)
    env["PORT"] = "abc"
    env["OPENCODE_GO_API_KEY"] = "x"
    proc = subprocess.run([sys.executable, RELAY_PY], env=env,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 1, proc.returncode
    assert "PORT must be a number" in proc.stderr, proc.stderr
    print("PASS bad env exits cleanly")


def test_anthropic_stream_midstream_error(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = True
    req["messages"] = [{"role": "user", "content": "TRIGGER_SSE_ERROR"}]
    status, text, ctype = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    assert "text/event-stream" in ctype
    assert "HTTP/1.1 502" not in text, "HTTP status line leaked into SSE stream"
    events = parse_sse_events(text)
    errors = [d for e, d in events if e == "error"]
    assert errors, "expected an SSE error event, got %s" % [e for e, _ in events]
    assert errors[0]["type"] == "error"
    print("PASS anthropic stream midstream error -> SSE error event")


def test_responses_stream_indices(base):
    req = dict(RESPONSES_REQUEST)
    req["stream"] = True
    req["input"] = [{"role": "user", "content": "TRIGGER_MULTI_TOOL"}]
    status, text, _ = post(base + "/v1/responses", req)
    assert status == 200, (status, text)
    events = parse_sse_events(text)
    added = [d for e, d in events if e == "response.output_item.added"]
    idxs = [d["output_index"] for d in added]
    assert idxs == [0, 1, 2], idxs
    done_fc = [d for e, d in events if e == "response.function_call_arguments.done"]
    assert [d["output_index"] for d in done_fc] == [1, 2], done_fc
    completed = [d for e, d in events if e == "response.completed"][0]
    types = [o["type"] for o in completed["response"]["output"]]
    assert types == ["message", "function_call", "function_call"], types
    print("PASS responses stream output_index increments")


def test_anthropic_tool_first_block_order(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = True
    req["messages"] = [{"role": "user", "content": "TRIGGER_TOOL_FIRST"}]
    status, text, _ = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    events = parse_sse_events(text)
    starts = [d for e, d in events if e == "content_block_start"]
    idxs = [d["index"] for d in starts]
    types = [d["content_block"]["type"] for d in starts]
    assert idxs == [0, 1] and types == ["tool_use", "text"], (idxs, types)
    stops = [d["index"] for e, d in events if e == "content_block_stop"]
    assert stops == [0, 1], stops
    md = [d for e, d in events if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use"
    print("PASS anthropic tool-first block ordering")


def test_anthropic_missing_tool_name(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = True
    req["messages"] = [{"role": "user", "content": "TRIGGER_MISSING_TOOL_NAME"}]
    status, text, _ = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    events = parse_sse_events(text)
    starts = [d for e, d in events if e == "content_block_start"]
    assert not [d for d in starts if d["content_block"]["type"] == "tool_use"]
    md = [d for e, d in events if e == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn", md
    print("PASS anthropic missing tool name -> end_turn")


def test_anthropic_html_upstream_error(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = False
    req["messages"] = [{"role": "user", "content": "TRIGGER_HTML_ERROR"}]
    r = urllib.request.Request(base + "/v1/messages",
                               data=json.dumps(req).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer secret")
    try:
        urllib.request.urlopen(r, timeout=30)
        assert False, "should have errored"
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        assert exc.code == 502
        msg = body["error"]["message"]
        assert msg == "upstream request failed", msg
        assert "<html" not in msg and "blocked" not in msg
    print("PASS upstream html error sanitized")


def test_anthropic_empty_content_fallback(base):
    req = dict(ANTHROPIC_REQUEST)
    req["stream"] = False
    req["messages"] = [{"role": "user", "content": "TRIGGER_EMPTY_CONTENT"}]
    status, text, _ = post(base + "/v1/messages", req)
    assert status == 200, (status, text)
    msg = json.loads(text)
    assert msg["content"] == [{"type": "text", "text": ""}], msg["content"]
    assert msg["stop_reason"] == "end_turn"
    print("PASS anthropic empty content fallback")


def test_stream_options_retry(base):
    MockUpstream.chat_requests.clear()
    req = dict(RESPONSES_REQUEST)
    req["stream"] = True
    req["input"] = [{"role": "user", "content": "TRIGGER_RETRY"}]
    status, text, _ = post(base + "/v1/responses", req)
    assert status == 200, (status, text)
    assert len(MockUpstream.chat_requests) == 2, len(MockUpstream.chat_requests)
    assert "stream_options" in MockUpstream.chat_requests[0]
    assert "stream_options" not in MockUpstream.chat_requests[1]
    print("PASS stream_options auto-retry")


def raw_post(port, request_head, body=b""):
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(request_head + body)
    s.shutdown(socket.SHUT_WR)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return data


def test_bad_content_length(base, port):
    host = base.split("://")[1]
    head = (
        "POST /v1/messages HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Authorization: Bearer secret\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: abc\r\n"
        "\r\n" % host
    ).encode("utf-8")
    data = raw_post(port, head)
    assert data.startswith(b"HTTP/1.1 400"), data[:120]
    print("PASS malformed Content-Length -> 400")


def test_chunked_rejected(base, port):
    host = base.split("://")[1]
    head = (
        "POST /v1/messages HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Authorization: Bearer secret\r\n"
        "Content-Type: application/json\r\n"
        "Transfer-Encoding: chunked\r\n"
        "\r\n" % host
    ).encode("utf-8")
    data = raw_post(port, head, body=b"0\r\n\r\n")
    assert data.startswith(b"HTTP/1.1 400"), data[:120]
    print("PASS chunked Transfer-Encoding -> 400")


def main():
    import tempfile
    mock, mock_port = start_mock()
    env = dict(os.environ)
    env.update({
        "OPENCODE_GO_API_KEY": "test-go-key",
        "RELAY_TOKEN": "secret",
        "DEFAULT_MODEL": "deepseek-v4-flash",
        "UPSTREAM_BASE": "http://127.0.0.1:%d/v1" % mock_port,
        "HOST": "127.0.0.1",
    })
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    relay_port = s.getsockname()[1]
    s.close()
    env["PORT"] = str(relay_port)
    err_log = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    err_log.close()
    proc = subprocess.Popen(
        [sys.executable, RELAY_PY],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(err_log.name, "wb"),
    )

    # 按请求取 key 模式: 不设置 OPENCODE_GO_API_KEY / RELAY_TOKEN
    env2 = dict(env)
    env2.pop("OPENCODE_GO_API_KEY", None)
    env2["RELAY_TOKEN"] = ""
    env2["CLIENT_TIMEOUT"] = "2"  # 测 slowloris 用短超时
    s2 = socket.socket()
    s2.bind(("127.0.0.1", 0))
    relay_port2 = s2.getsockname()[1]
    s2.close()
    env2["PORT"] = str(relay_port2)
    err_log2 = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    err_log2.close()
    proc2 = subprocess.Popen(
        [sys.executable, RELAY_PY],
        env=env2,
        stdout=subprocess.DEVNULL,
        stderr=open(err_log2.name, "wb"),
    )

    # 按请求取 key + RELAY_TOKEN 门禁模式
    env3 = dict(env2)
    env3["RELAY_TOKEN"] = "relay-gate"
    s3 = socket.socket()
    s3.bind(("127.0.0.1", 0))
    relay_port3 = s3.getsockname()[1]
    s3.close()
    env3["PORT"] = str(relay_port3)
    err_log3 = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    err_log3.close()
    proc3 = subprocess.Popen(
        [sys.executable, RELAY_PY],
        env=env3,
        stdout=subprocess.DEVNULL,
        stderr=open(err_log3.name, "wb"),
    )
    try:
        base = "http://127.0.0.1:%d" % relay_port
        if not wait_ready(relay_port):
            with open(err_log.name, "rb") as f:
                sys.stderr.write("RELAY STDERR:\n" + f.read().decode("utf-8", "replace") + "\n")
            assert False, "relay did not start"
        test_auth(base)
        test_models(base)
        test_anthropic_nonstream(base)
        test_anthropic_stream(base)
        test_responses_nonstream(base)
        test_responses_stream(base)
        test_chat_passthrough(base)
        test_anthropic_stream_midstream_error(base)
        test_responses_stream_indices(base)
        test_anthropic_tool_first_block_order(base)
        test_anthropic_missing_tool_name(base)
        test_anthropic_html_upstream_error(base)
        test_anthropic_empty_content_fallback(base)
        test_stream_options_retry(base)
        test_bad_content_length(base, relay_port)
        test_chunked_rejected(base, relay_port)
        test_nonjson_upstream(base)
        test_length_finish_reason(base)
        test_html_sse_guard(base)

        base2 = "http://127.0.0.1:%d" % relay_port2
        if not wait_ready(relay_port2):
            with open(err_log2.name, "rb") as f:
                sys.stderr.write("RELAY2 STDERR:\n" + f.read().decode("utf-8", "replace") + "\n")
            assert False, "per-request relay did not start"
        test_per_request_key(base2)
        test_client_socket_timeout(base2, relay_port2)
        test_sigterm_graceful(proc2)

        base3 = "http://127.0.0.1:%d" % relay_port3
        if not wait_ready(relay_port3):
            with open(err_log3.name, "rb") as f:
                sys.stderr.write("RELAY3 STDERR:\n" + f.read().decode("utf-8", "replace") + "\n")
            assert False, "gated relay did not start"
        test_relay_token_not_forwarded(base3)

        test_bad_env_exits_cleanly()
        print("\nAll tests passed.")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        if proc2.poll() is None:
            proc2.terminate()
            proc2.wait(timeout=5)
        proc3.terminate()
        proc3.wait(timeout=5)
        mock.shutdown()
        mock.server_close()


if __name__ == "__main__":
    main()
