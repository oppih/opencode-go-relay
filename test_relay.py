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

        if not payload.get("stream"):
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

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

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
             "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}},
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
        "PORT": "0",  # 不支持 0, 下面用固定端口
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
        print("\nAll tests passed.")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        mock.shutdown()
        mock.server_close()


if __name__ == "__main__":
    main()
