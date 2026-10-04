# Changelog

Changes carried by this branch (`feat/anthropic-native-passthrough`), relative to upstream
`main` (85c39f1). The fork's `main` branch additionally carries `MODELS_EXTRA` (PR #1) and a
process-stable session fallback (PR #2); both are separate changes and intentionally absent here,
so this branch is a single-topic patch.

## [feat/anthropic-native-passthrough]

### Added
- **Anthropic passthrough** (`ANTHROPIC_PASSTHROUGH=1`, opt-in): `/v1/messages` forwards the
  request body byte-faithfully to the upstream Anthropic-native endpoint
  (`<UPSTREAM_BASE>/messages`, authenticated with `x-api-key`) and streams the response back
  verbatim. Prompt caching (`cache_creation_input_tokens` / `cache_read_input_tokens`), thinking
  blocks and native `tool_use` (including streaming `input_json_delta`) survive, which a
  Messages ⇄ chat/completions translation cannot carry. The response path is dispatched per
  request, so `/v1/chat/completions` and `/v1/responses` are untouched. Default is `0`, so an
  existing deployment keeps its path — and because only models that speak the Anthropic protocol
  work in this mode (others answer `400 ModelProtocolUnsupported`), turning it on is a deliberate
  choice.
- **`POST /v1/messages/count_tokens`** (both modes). The upstream answers 404, so the relay counts
  locally: text (including `system`), `tool_result` / `tool_use` payloads and `tools` definitions
  count; thinking and image blocks do not. Uses `tiktoken` `o200k_base` when installed (optional
  dependency, imported lazily) and falls back to `len / 3.6`. Documented as an estimate, not an
  authoritative count.
- `tools/contract_check.py`: live PASS/GAP/FAIL check of every endpoint against a running relay
  (auth, models, both Anthropic modes, tool use, `count_tokens`, cache fields, error surface).
- `test_passthrough.py`: standalone suite for the new mode (mock native upstream, both switch
  positions, byte fidelity, incremental delivery, session priority, model default, error
  passthrough, `count_tokens`).
- README: passthrough and token-counting documentation, deployment verification, troubleshooting.
- Examples: full `.env.example` (with `ANTHROPIC_PASSTHROUGH`, `STREAM_CHUNK`,
  `UPSTREAM_ERROR_BODY_LIMIT`, `UPSTREAM_UA`, `CLIENT_TIMEOUT`, `MAX_CONNECTIONS`) and an
  `examples/README.md` index.

### Fixed
- **Streaming was fully buffered** in passthrough mode: `HTTPResponse.read(n)` returns only when it
  has n bytes or the stream ends (true for chunked bodies too), so SSE events reached the client
  all at once when the upstream closed. Measured with a mock upstream emitting an event every
  0.8 s: `[3.2, 3.2, 3.2, 3.2]` instead of `[0.0, 0.8, 1.6, 2.4]`. Now uses `read1`.
- **Upstream errors were rewritten instead of forwarded**: the status and the body survived, but
  `Content-Type` was forced to JSON, the body was cut at 64 KiB and `Retry-After` / request-id
  headers were dropped (a 100 KB HTML error arrived as a 65536-byte "JSON" body). Errors now keep
  status, `Content-Type`, custom headers and the full body, bounded by `UPSTREAM_ERROR_BODY_LIMIT`.
- **Read failures were swallowed** (`except OSError: pass`): when the upstream stalled after sending
  headers, the client received zero bytes and no error and nothing was logged. Now: a structured
  502 before headers are sent, an explicit connection close plus a log line afterwards.
- **`count_tokens` invented values**: malformed bodies and internal failures both returned
  `200 {"input_tokens": 1}`, reporting a long context as an empty one. Now 400 for malformed input,
  500 for internal failure, and the count is never fabricated.
- **A body without `model` was forwarded as-is** in passthrough mode (upstream error); it now gets
  `DEFAULT_MODEL` like the translation path.
- **Relay-level 401 for a missing upstream key** used a non-Anthropic envelope.

## [upstream/main 85c39f1] — baseline

Fork base: translation bridge for `/v1/messages` (Anthropic) and `/v1/responses` (Responses) to
`chat/completions`, plus `/v1/models`, `/healthz`, token auth, a 64 MB body cap and a
`stream_options` fallback for picky upstreams.
