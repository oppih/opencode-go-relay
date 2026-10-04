# Changelog

All notable changes to this fork. Upstream (`youyoulyz/opencode-go-relay`) is the baseline; this
file exists so the fork is self-describing: what was added, why, and which branch carries it.

## [fork/main] — running deployment

### Added
- **Anthropic passthrough** (`ANTHROPIC_PASSTHROUGH=1`, opt-in): `/v1/messages` forwards the
  request body byte-faithfully to the upstream Anthropic-native endpoint (`<UPSTREAM_BASE>/messages`,
  `x-api-key` auth) and streams the response back verbatim. Prompt caching
  (`cache_creation_input_tokens` / `cache_read_input_tokens`), thinking blocks and native
  `tool_use` (including streaming `input_json_delta`) survive, which a Messages ⇄
  chat/completions translation cannot carry. Default is `0` so existing deployments keep their
  path; only models that speak the Anthropic protocol work in this mode.
- **`POST /v1/messages/count_tokens`** (both modes). The upstream answers 404, so the relay counts
  locally: text (including `system`), `tool_result` / `tool_use` payloads and `tools` definitions
  count; thinking and image blocks do not. Uses `tiktoken` `o200k_base` when installed (optional
  dependency) and falls back to `len / 3.6`. Documented as an estimate, not an authoritative count.
- **`MODELS_EXTRA`**: extra model ids advertised by `/v1/models` (submitted upstream as PR #1).
- **`RELAY_SESSION_ID`**: process-stable fallback for the session id sent upstream. Client headers
  (`x-opencode-session`, Claude Code's `X-Claude-Code-Session-Id`) still take priority; without a
  stable fallback every request is a new session, which means a cold prompt cache every time
  (related change submitted upstream as PR #2).
- `tools/contract_check.py`: live PASS/GAP/FAIL check of every endpoint against a running relay.
- README: passthrough/count_tokens documentation, full env-var table, troubleshooting table,
  deployment verification, fork notes.

### Fixed
- **Streaming was fully buffered** in passthrough mode: `HTTPResponse.read(n)` returns only when it
  has n bytes or the stream ends (true for chunked bodies too), so SSE events reached the client
  all at once when the upstream closed. Measured with a mock upstream emitting an event every
  0.8 s: `[3.2, 3.2, 3.2, 3.2]` instead of `[0.0, 0.8, 1.6, 2.4]`. Now uses `read1`; the
  translation path's SSE reader had the same defect and was fixed with it.
- **Upstream errors were rewritten instead of forwarded**: status and body survived, but
  `Content-Type` was forced to JSON, the body was cut at 64 KiB and `Retry-After` / request-id
  headers were dropped (a 100 KB HTML error arrived as a 65536-byte "JSON" body). Errors now keep
  status, `Content-Type`, custom headers and the full body, bounded by `UPSTREAM_ERROR_BODY_LIMIT`.
- **Read failures were swallowed** (`except OSError: pass`): if the upstream stalled after sending
  headers the client received zero bytes and no error, and nothing was logged. Now: structured 502
  before headers are sent, explicit connection close plus a log line after.
- **`count_tokens` invented values**: malformed bodies and internal failures both returned
  `200 {"input_tokens": 1}`, which reports a long context as an empty one. Now 400 for malformed
  input, 500 for internal failure, and the count is never fabricated.
- **A body without `model` was forwarded as-is** in passthrough mode (upstream error); it now gets
  `DEFAULT_MODEL` like the translation path.
- **Relay-level 401 used a non-Anthropic envelope** for a missing upstream key.

### Changed
- `ANTHROPIC_PASSTHROUGH` defaults to `0` (opt-in), so a deployment whose `UPSTREAM_BASE` serves
  only `chat/completions` never silently changes path.
- Tests: `test_relay.py` gained a native `/messages` endpoint in its mock upstream plus cases for
  byte fidelity, incremental delivery, session priority, model default, error passthrough and
  `count_tokens`; the original suite still passes unchanged.

## [upstream/main 85c39f1] — baseline

Fork base: translation bridge for `/v1/messages` (Anthropic) and `/v1/responses` (Responses) to
`chat/completions`, `/v1/models`, `/healthz`, token auth, 64 MB body cap, `stream_options`
fallback for picky upstreams.
