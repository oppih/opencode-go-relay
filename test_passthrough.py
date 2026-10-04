#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the opt-in Anthropic passthrough (ANTHROPIC_PASSTHROUGH=1).

Standalone on purpose: it starts a mock upstream that speaks the Anthropic-native protocol
and two relay instances (translation mode and passthrough mode) on the same mock, so the
switch itself is covered as well. No network access.

Run: python3 test_passthrough.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RELAY_PY = os.path.join(SCRIPT_DIR, "relay.py")
TOKEN = "secret"
NATIVE_HTML = b"<html><body>" + b"upstream error page " * 5000 + b"</body></html>"


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    native_requests = []
    chat_requests = []

    def log_message(self, *args):
        pass

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw.decode("utf-8"))
        user_text = "\n".join(
            str(m.get("content", ""))
            for m in payload.get("messages") or []
            if isinstance(m.get("content"), str)
        )

        if self.path.rstrip("/").endswith("/messages"):
            MockUpstream.native_requests.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "raw": raw,
                "payload": payload,
            })
            if "TRIGGER_HTML_ERROR" in user_text:
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Retry-After", "7")
                self.send_header("x-request-id", "rid-mock-1")
                self.send_header("Content-Length", str(len(NATIVE_HTML)))
                self.end_headers()
                self.wfile.write(NATIVE_HTML)
                return
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for index in range(3):
                    self.wfile.write(('event: content_block_delta\ndata: {"i": %d}\n\n' % index).encode())
                    self.wfile.flush()
                    time.sleep(0.6)
                self.close_connection = True
                return
            self._send(200, "application/json", json.dumps({
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": payload.get("model"),
                "content": [{"type": "text", "text": "native ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 3,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 7},
            }).encode("utf-8"))
            return

        MockUpstream.chat_requests.append(payload)
        self._send(200, "application/json", json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
            "model": payload.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "translated"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }).encode("utf-8"))


def start_mock():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_ready(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def payload_with_model():
    return {
        "model": "deepseek-v4.1-flash",
        "max_tokens": 8,
        "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "t", "input_schema": {"type": "object"}}],
    }


def post(base, path, payload, headers=None, timeout=30):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + TOKEN)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")


def test_off_is_translate(base):
    before = len(MockUpstream.native_requests)
    status, body, _ = post(base, "/v1/messages",
                           {"model": "m-translate", "max_tokens": 8,
                            "messages": [{"role": "user", "content": "hello"}]})
    assert status == 200
    assert len(MockUpstream.native_requests) == before, "switch off must not hit the native endpoint"
    assert MockUpstream.chat_requests[-1]["model"] == "m-translate"
    print("PASS switch off -> translation path")


def test_nonstream(base):
    payload = payload_with_model()
    status, body, _ = post(base, "/v1/messages", payload)
    assert status == 200
    data = json.loads(body)
    assert data["content"][0]["text"] == "native ok"
    assert data["usage"]["cache_read_input_tokens"] == 7, "native cache fields must survive"
    record = MockUpstream.native_requests[-1]
    assert record["path"].rstrip("/").endswith("/messages")
    assert json.loads(record["raw"].decode("utf-8")) == payload, "request body must be byte-faithful"
    assert record["headers"].get("x-api-key"), "native endpoint needs x-api-key"
    assert record["headers"].get("anthropic-version") == "2023-06-01"
    print("PASS passthrough non-stream (byte fidelity + cache fields)")


def test_stream(base):
    payload = dict(payload_with_model())
    payload["stream"] = True
    request = urllib.request.Request(base + "/v1/messages",
                                     data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + TOKEN)
    started = time.time()
    first_event = None
    seen = b""
    with urllib.request.urlopen(request, timeout=30) as resp:
        while True:
            line = resp.readline()
            if not line:
                break
            seen += line
            if line.strip().startswith(b"data:") and first_event is None:
                first_event = time.time() - started
    total = time.time() - started
    assert seen.count(b"data:") == 3, seen
    assert first_event is not None, "no event arrived"
    assert first_event < total - 0.5, "events were buffered until the stream ended: %.2f vs %.2f" % (
        first_event, total)
    print("PASS passthrough stream (byte fidelity + incremental delivery)")


def test_session_priority(base):
    payload = payload_with_model()
    post(base, "/v1/messages", payload,
         headers={"X-Claude-Code-Session-Id": "cc-1", "x-opencode-session": "cli-1"})
    assert MockUpstream.native_requests[-1]["headers"]["x-opencode-session"] == "cc-1"
    post(base, "/v1/messages", payload, headers={"x-opencode-session": "cli-2"})
    assert MockUpstream.native_requests[-1]["headers"]["x-opencode-session"] == "cli-2"
    post(base, "/v1/messages", payload)
    fallback = MockUpstream.native_requests[-1]["headers"]["x-opencode-session"]
    assert fallback and fallback.strip(), "a session id must always be sent (cache affinity)"
    print("PASS session priority (Claude Code header > client header > fallback)")


def test_model_default(base):
    post(base, "/v1/messages", {"max_tokens": 8,
                                "messages": [{"role": "user", "content": "hello"}]})
    forwarded = json.loads(MockUpstream.native_requests[-1]["raw"].decode("utf-8"))
    assert forwarded["model"] == "deepseek-v4-flash", forwarded
    print("PASS model-less request gets DEFAULT_MODEL")


def test_error_passthrough(base):
    request = urllib.request.Request(
        base + "/v1/messages",
        data=json.dumps({"model": "m", "max_tokens": 8,
                         "messages": [{"role": "user", "content": "TRIGGER_HTML_ERROR"}]}).encode(),
        method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + TOKEN)
    try:
        urllib.request.urlopen(request, timeout=30)
        assert False, "an upstream 500 must not look like a success"
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        assert exc.code == 500, exc.code
        assert exc.headers.get("Content-Type", "").startswith("text/html"), exc.headers.get("Content-Type")
        assert exc.headers.get("Retry-After") == "7"
        assert exc.headers.get("x-request-id") == "rid-mock-1"
        assert len(raw) == len(NATIVE_HTML), (len(raw), len(NATIVE_HTML))
    print("PASS upstream error passthrough (status + type + headers + full body)")


def test_count_tokens(base):
    status, body, _ = post(base, "/v1/messages/count_tokens",
                           {"model": "m", "messages": [{"role": "user", "content": "x" * 4000}]})
    assert status == 200
    assert json.loads(body)["input_tokens"] > 500, "long text must not look empty"
    for bad in ({"model": "m"}, {"model": "m", "messages": "nope"}):
        request = urllib.request.Request(base + "/v1/messages/count_tokens",
                                         data=json.dumps(bad).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + TOKEN)
        try:
            urllib.request.urlopen(request, timeout=30)
            assert False, "malformed body must be 400: %r" % (bad,)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, exc.code
            assert json.loads(exc.read())["error"]["type"] == "invalid_request_error"
    print("PASS count_tokens (estimate + 400 on malformed body)")


def main():
    mock, mock_port = start_mock()
    env = dict(os.environ)
    env.update({
        "OPENCODE_GO_API_KEY": "test-go-key",
        "RELAY_TOKEN": TOKEN,
        "DEFAULT_MODEL": "deepseek-v4-flash",
        "UPSTREAM_BASE": "http://127.0.0.1:%d/v1" % mock_port,
        "HOST": "127.0.0.1",
    })
    procs, logs = [], []
    try:
        environments = [("translate", env), ("passthrough", dict(env, ANTHROPIC_PASSTHROUGH="1"))]
        ports = []
        for name, environment in environments:
            port = free_port()
            environment = dict(environment, PORT=str(port))
            import tempfile
            log = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
            log.close()
            logs.append(log.name)
            procs.append(subprocess.Popen([sys.executable, RELAY_PY], env=environment,
                                          stdout=subprocess.DEVNULL,
                                          stderr=open(log.name, "wb")))
            ports.append(port)
        translate_base = "http://127.0.0.1:%d" % ports[0]
        passthrough_base = "http://127.0.0.1:%d" % ports[1]
        for port, name, log in zip(ports, ("translate", "passthrough"), logs):
            if not wait_ready(port):
                sys.stderr.write("relay (%s) did not start; log %s:\n%s\n" % (
                    name, log, open(log, "rb").read().decode("utf-8", "replace")))
                assert False, "relay did not start"

        test_off_is_translate(translate_base)
        test_nonstream(passthrough_base)
        test_stream(passthrough_base)
        test_session_priority(passthrough_base)
        test_model_default(passthrough_base)
        test_error_passthrough(passthrough_base)
        test_count_tokens(passthrough_base)
        print("\nAll passthrough tests passed.")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        mock.shutdown()
        mock.server_close()


if __name__ == "__main__":
    main()
