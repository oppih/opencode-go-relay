#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Contract check for a running relay: exercise every endpoint over HTTP and report
PASS / GAP / FAIL per check.

Unlike test_relay.py (which mocks the upstream), this talks to a live relay and its real
upstream, so it costs tokens. It answers the question "is this deployment actually serving
what the docs promise?", which a service-status check cannot: the relay can be up, healthy and
still be failing every request (wrong key, wrong mode, upstream 401).

Usage:
    export RELAY_BASE=http://127.0.0.1:8787
    export RELAY_TOKEN=<RELAY_TOKEN>
    export MODEL=deepseek-v4-flash          # optional, defaults to deepseek-v4-flash
    python3 tools/contract_check.py

Exit code: 0 when no FAIL, 1 otherwise (GAPs do not fail the run).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("RELAY_BASE", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
MODEL = os.environ.get("MODEL", "deepseek-v4-flash").strip()
TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))

results = []


def record(kind, name, detail):
    results.append((kind, name, detail))
    print("[%s] %s: %s" % (kind, name, detail))


def request(path, payload=None, headers=None, method=None, timeout=TIMEOUT):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return urllib.request.urlopen(req, timeout=timeout)


def auth_headers():
    return {"Authorization": "Bearer " + TOKEN} if TOKEN else {}


def check_health():
    with request("/healthz") as resp:
        body = json.loads(resp.read() or b"{}")
    record("PASS" if resp.status == 200 and body.get("ok") else "FAIL",
           "GET /healthz", "%s %s" % (resp.status, body))


def check_models():
    with request("/v1/models", headers=auth_headers()) as resp:
        body = json.loads(resp.read())
    ids = [item.get("id") for item in body.get("data") or []]
    record("PASS" if resp.status == 200 and ids else "FAIL", "GET /v1/models",
           "%s %s" % (resp.status, ids))


def check_nonstream():
    payload = {"model": MODEL, "max_tokens": 16,
               "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}
    with request("/v1/messages", payload, auth_headers()) as resp:
        body = json.loads(resp.read())
    types = [block.get("type") for block in body.get("content") or []]
    ok = resp.status == 200 and bool(body.get("content")) and "usage" in body
    record("PASS" if ok else "FAIL", "POST /v1/messages (non-stream)",
           "%s content_types=%s usage=%s" % (resp.status, types, body.get("usage")))


def check_stream():
    payload = {"model": MODEL, "max_tokens": 32, "stream": True,
               "messages": [{"role": "user", "content": "Count to five."}]}
    events, first, started = [], None, time.time()
    with request("/v1/messages", payload, auth_headers()) as resp:
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"event:"):
                events.append(line.split(b":", 1)[1].strip().decode("utf-8", "replace"))
            if first is None and line.startswith(b"data:"):
                first = round(time.time() - started, 2)
    ok = "message_start" in events and "message_stop" in events
    record("PASS" if ok else "FAIL", "POST /v1/messages (stream)",
           "%s events=%d first_data=%ss has_start/stop=%s" % (
               resp.status, len(events), first, ("message_start" in events, "message_stop" in events)))


def check_tool_use():
    payload = {"model": MODEL, "max_tokens": 64,
               "tools": [{"name": "get_weather", "description": "Weather for a city",
                          "input_schema": {"type": "object",
                                           "properties": {"city": {"type": "string"}},
                                           "required": ["city"]}}],
               "messages": [{"role": "user", "content": "Use the get_weather tool for Shanghai."}]}
    try:
        with request("/v1/messages", payload, auth_headers()) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        record("GAP", "tool_use", "%s %s" % (exc.code, exc.read()[:160]))
        return
    types = [block.get("type") for block in body.get("content") or []]
    if "tool_use" in types:
        record("PASS", "tool_use", "content_types=%s stop=%s" % (types, body.get("stop_reason")))
    else:
        record("GAP", "tool_use", "model answered without a tool_use block: %s" % types)


def check_count_tokens():
    try:
        with request("/v1/messages/count_tokens",
                     {"model": MODEL, "messages": [{"role": "user", "content": "x" * 400}]},
                     auth_headers()) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        record("GAP", "POST /v1/messages/count_tokens",
               "%s %s (Claude Code calls this endpoint; implement or expect it to fail)" % (
                   exc.code, exc.read()[:120]))
        return
    tokens = body.get("input_tokens")
    record("PASS" if resp.status == 200 and isinstance(tokens, int) and tokens > 0 else "FAIL",
           "POST /v1/messages/count_tokens", "%s input_tokens=%s (estimate)" % (resp.status, tokens))


def check_cache_control():
    payload = {"model": MODEL, "max_tokens": 8,
               "system": [{"type": "text", "text": "You are terse." * 100,
                           "cache_control": {"type": "ephemeral"}}],
               "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}
    try:
        with request("/v1/messages", payload, auth_headers()) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        record("FAIL", "cache_control", "%s %s" % (exc.code, exc.read()[:160]))
        return
    usage = body.get("usage") or {}
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        record("PASS", "prompt caching fields", "usage=%s" % usage)
    else:
        record("GAP", "prompt caching fields",
               "accepted but usage has no cache fields: %s (translation mode drops them; "
               "ANTHROPIC_PASSTHROUGH=1 keeps the upstream's)" % usage)


def check_unknown_model():
    payload = {"model": "no-such-model-xyz", "max_tokens": 8,
               "messages": [{"role": "user", "content": "hi"}]}
    try:
        with request("/v1/messages", payload, auth_headers()) as resp:
            record("FAIL", "error surface (unknown model)", "unexpectedly succeeded: %s" % resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        shape = "anthropic" if parsed.get("type") == "error" and parsed.get("error", {}).get("type") else "other"
        record("PASS" if shape == "anthropic" else "GAP",
               "error surface (unknown model)", "%s shape=%s %s" % (exc.code, shape, body[:120]))


def check_auth():
    try:
        with request("/v1/messages", {"model": MODEL, "max_tokens": 4,
                                      "messages": [{"role": "user", "content": "hi"}]}) as resp:
            record("FAIL", "auth failure (no token)", "request without a token succeeded")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        record("PASS" if exc.code == 401 and parsed.get("type") == "error" and
               parsed.get("error", {}).get("type") == "authentication_error" else "GAP",
               "auth failure (no token)", "%s %s" % (exc.code, body[:140]))


def main():
    if not TOKEN:
        print("RELAY_TOKEN is not set - checks that need auth will report what the relay returns.")
    print("relay: %s  model: %s\n" % (BASE, MODEL))
    for check in (check_health, check_models, check_nonstream, check_stream, check_tool_use,
                  check_count_tokens, check_cache_control, check_unknown_model, check_auth):
        try:
            check()
        except Exception as exc:  # keep going: one broken endpoint must not hide the rest
            record("FAIL", check.__name__, "%s: %s" % (type(exc).__name__, exc))
    counts = {kind: sum(1 for k, _, _ in results if k == kind) for kind in ("PASS", "GAP", "FAIL")}
    print("\n=== summary: PASS %d / GAP %d / FAIL %d ===" % (counts["PASS"], counts["GAP"], counts["FAIL"]))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
