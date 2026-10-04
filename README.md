# OpenCode Go Relay

A protocol bridge that lets **Claude Code** (Anthropic Messages) and **Codex** (OpenAI Responses) share the models in your [OpenCode Go](https://opencode.ai) subscription — default model `deepseek-v4-flash`. Pure Python standard library, single file; the only optional third-party dependency is `tiktoken`, used to improve the local `/v1/messages/count_tokens` estimate.

> 中文简介:一个协议中转站,让 Claude Code(Anthropic Messages 协议)和 Codex(OpenAI Responses 协议)共用 OpenCode Go 订阅里的模型。纯 Python 标准库,单文件;唯一可选的第三方依赖是 `tiktoken`(用于改进本地 `/v1/messages/count_tokens` 估算)。

## Why?

OpenCode Go exposes both an OpenAI-compatible endpoint (`https://opencode.ai/zen/go/v1/chat/completions`) and an **Anthropic-native endpoint** (`https://opencode.ai/zen/go/v1/messages`, authenticated with `x-api-key`). The two are not equivalent:

- The native endpoint returns what Anthropic clients actually need — `cache_creation_input_tokens` / `cache_read_input_tokens` (prompt caching), thinking blocks, native `tool_use` (including streaming `input_json_delta`) and `anthropic-beta` semantics. Translating Messages ⇄ chat/completions necessarily drops that information.
- Only some models support the native protocol; others (e.g. `longcat-2.5-preview-free`) answer `400 ModelProtocolUnsupported` there and must go through `chat/completions`.

So the relay supports both: an opt-in **Anthropic passthrough** (`ANTHROPIC_PASSTHROUGH=1`) that forwards `/v1/messages` to the native endpoint byte-for-byte, and the original translation path as the default.


## Features

- **`POST /v1/messages`** — two modes:
  - *passthrough* (`ANTHROPIC_PASSTHROUGH=1`, opt-in): bytes forwarded to the upstream Anthropic-native endpoint, response bytes streamed back verbatim — prompt-cache fields, thinking, tool_use and `anthropic-beta` stay native; upstream errors keep their status, `Content-Type` and `Retry-After`/request-id headers
  - *translation* (default) (Anthropic) ⇄ upstream `chat/completions`
    - system blocks, tool use / tool results, images (URL + base64), thinking blocks
    - `tool_choice` mapping (`any` → `required`, named tool, etc.)
    - full SSE streaming translation: `message_start`, `content_block_delta`, `input_json_delta`, `message_delta`, `message_stop`
- **`POST /v1/messages/count_tokens`** — the upstream has no such endpoint (404), so the relay estimates locally (see below)

- **`POST /v1/responses`** (Responses) ⇄ upstream `chat/completions`
  - `instructions` → system, `function_call` / `function_call_output` items, `max_output_tokens` → `max_tokens`
  - SSE translation: `response.created`, `output_text.delta`, `function_call_arguments.delta`, `response.completed`
- **`POST /v1/chat/completions`** — pass-through (auth only; the client's model is forwarded, defaulting to `DEFAULT_MODEL` when omitted)
- **`GET /v1/models`**, **`GET /healthz`**
- Token auth (`RELAY_TOKEN`), 64 MB body cap, `stream_options` auto-retry fallback for picky upstreams
- Python 3.9+, standard library only

## Quick start

```bash
# on your public server
export OPENCODE_GO_API_KEY="..."     # your OpenCode Go key (stays on the server)
export RELAY_TOKEN="change-me"       # clients must send this (strongly recommended)
python3 relay.py                     # listens on 0.0.0.0:8787
```

Test it locally with a mocked upstream (no network, no real key needed):

```bash
python3 test_relay.py
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_GO_API_KEY` | *(empty)* | Optional. If set, all requests share this single key (server-key mode). If empty, every request must carry its own key (`Authorization: Bearer <key>` or `x-api-key: <key>`), which is forwarded upstream as-is. |
| `RELAY_TOKEN` | *(empty)* | Optional client auth token for your own access control. **Set it (or restrict by IP) before exposing publicly** — without it anyone can use your relay. In per-request key mode, leave it empty so the client's own key is the credential. |
| `DEFAULT_MODEL` | `deepseek-v4-flash` | Fallback model used when the client omits one; also advertised in `/v1/models`. Clients can request any OpenCode Go model (e.g. `glm-5.2`) and it is forwarded as-is. |
| `MODELS_EXTRA` | *(empty)* | Comma-separated extra model IDs advertised in `/v1/models` alongside `DEFAULT_MODEL` (e.g. `deepseek-v4-pro,glm-5.2`). Useful when clients discover models from `/v1/models` (e.g. Claude Code with `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`). Does not restrict what clients can request — any model is still forwarded as-is. |
| `UPSTREAM_BASE` | `https://opencode.ai/zen/go/v1` | Upstream base URL. In passthrough mode it must also serve the Anthropic-native `POST /messages` (it appends `/messages`). |
| `HOST` / `PORT` | `0.0.0.0` / `8787` | Listen address. |
| `ANTHROPIC_PASSTHROUGH` | `0` | `1` = forward `/v1/messages` to the upstream Anthropic-native endpoint instead of translating it to `chat/completions` (opt-in; see the passthrough section below for what it changes). |
| `RELAY_SESSION_ID` | *(random per process)* | Value sent upstream as `x-opencode-session` when the client sends none. The upstream binds prompt-cache affinity to this id, so a stable value lets repeat requests hit the cache. |
| `STREAM_CHUNK` | `8192` | Read size used when forwarding streamed bytes. Kept as a knob only because the reader switches to `read1`, which returns as soon as data arrives. |
| `UPSTREAM_ERROR_BODY_LIMIT` | `1048576` | Max bytes of an upstream error body forwarded in passthrough mode (larger bodies are truncated). |
| `STREAM_OPTIONS` | `1` | Send `stream_options.include_usage` upstream in the translation path; set `0` if the upstream rejects it. Ignored by passthrough (the body is forwarded untouched). |
| `REQUEST_TIMEOUT` | `600` | Upstream request timeout (seconds). |
| `UPSTREAM_UA` | *(browser UA)* | User-Agent sent upstream; some edges (e.g. Cloudflare) reject urllib's default. |
| `MAX_CONNECTIONS` | `64` | Max concurrent connections; excess connections wait (backpressure). |
| `CLIENT_TIMEOUT` | `60` | Per-connection read timeout in seconds; idle connections (slowloris) are dropped. |

### Endpoints

| Path | Protocol | Translation |
|---|---|---|
| `POST /v1/messages` | Anthropic | passthrough to upstream `/messages` when `ANTHROPIC_PASSTHROUGH=1`, else → `chat/completions` |
| `POST /v1/messages/count_tokens` | Anthropic | local estimate (upstream has no such endpoint) |
| `POST /v1/responses` | Responses | → `chat/completions` |
| `POST /v1/chat/completions` | OpenAI | passthrough (model rewritten) |
| `GET /v1/models` | OpenAI | model list |
| `GET /healthz` | — | health check (no auth) |

### Anthropic passthrough (`ANTHROPIC_PASSTHROUGH=1`)

What changes on `/v1/messages`:

- the request body is forwarded byte-for-byte to `UPSTREAM_BASE + /messages` and authenticated with `x-api-key` (the native endpoint rejects `Authorization: Bearer` alone). The one exception: if the body has no `model`, `DEFAULT_MODEL` is filled in, exactly like the translation path.
- the response is streamed back verbatim (no re-framing, no re-typing), so `cache_creation_input_tokens` / `cache_read_input_tokens`, thinking blocks, native `tool_use` and `anthropic-beta` behaviour are the upstream's, not the relay's.
- an upstream error is forwarded with its status code, its `Content-Type` (including `text/html`), `Retry-After` / request-id headers and full body (up to `UPSTREAM_ERROR_BODY_LIMIT`).
- a request without a client session id gets `RELAY_SESSION_ID`; Claude Code's `X-Claude-Code-Session-Id` (or the client's `x-opencode-session`) takes priority.
- only models that support the Anthropic protocol work here — others answer `400 ModelProtocolUnsupported`. Leave the switch at `0` for those.

`/v1/messages/count_tokens` is **not** controlled by this switch and is available either way. The upstream has no such endpoint (404), so the relay counts locally: text (including `system`), `tool_result` / `tool_use` payloads and `tools` definitions count; thinking and image blocks do not. Counting uses `tiktoken`'s `o200k_base` when installed (`pip install tiktoken`) and falls back to `len / 3.6` otherwise. Either way it is an **estimate** — the upstream tokenizer differs, so treat the number as an order-of-magnitude hint, not an authoritative count. Invalid request bodies get `400`, and estimation failures get `500` — never a fake `input_tokens`.


Auth: every request except `/healthz` must carry a key. In per-request key mode that key is the client's own OpenCode Go key (sent as `Authorization: Bearer <key>` or `x-api-key: <key>`) and is forwarded upstream. If `RELAY_TOKEN` is set, it acts as an additional gate: send it in one of the two headers and the upstream key in the other.

### Per-request API key mode (recommended for Claude Code)

Leave `OPENCODE_GO_API_KEY` unset. Claude Code already authenticates with
`ANTHROPIC_AUTH_TOKEN`, so you only need to change the base URL — the key you
already have is your OpenCode Go key, and the relay forwards it untouched:

```bash
export ANTHROPIC_BASE_URL="https://your-server:8558/opencode-go/anthropic"
export ANTHROPIC_AUTH_TOKEN="<your own OpenCode Go key>"
export ANTHROPIC_MODEL="deepseek-v4-flash"
claude
```

> 中文:不设置 `OPENCODE_GO_API_KEY` 时, relay 从每个请求里取 key 并原样转发上游,
> 服务器不保存任何 key。Claude Code 的 `ANTHROPIC_AUTH_TOKEN` 就是这个 key,
> 所以只需要把 `ANTHROPIC_BASE_URL` 指向 relay, 其余全部照旧。

Same for Codex: keep your OpenCode Go key as `OPENCODE_GO_API_KEY` in your
provider config and point `base_url` at the relay.

## Client setup

### Claude Code (via the relay)

```bash
export ANTHROPIC_BASE_URL="http://your-server:8787"
export ANTHROPIC_AUTH_TOKEN="<RELAY_TOKEN>"
export ANTHROPIC_MODEL="deepseek-v4-flash"   # optional alias
```

Or in `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://your-server:8787",
    "ANTHROPIC_AUTH_TOKEN": "<RELAY_TOKEN>",
    "ANTHROPIC_MODEL": "deepseek-v4-flash"
  }
}
```

> 中文:Claude Code 通过环境变量指向 relay(`ANTHROPIC_BASE_URL`),relay 把 Messages 协议翻译成上游 chat/completions。

With `ANTHROPIC_PASSTHROUGH=1` (see above) the relay forwards Messages to the upstream native endpoint instead. That is what makes prompt caching visible to Claude Code: `cacheReadInputTokens` only appears when the same session id is reused, which is why the relay forwards Claude Code's own session id (or `RELAY_SESSION_ID`). Two caveats worth knowing: Claude Code's `cost` figure is its own price-table estimate for a model it doesn't know — it is not the upstream's billing — and only models that speak the Anthropic protocol work in passthrough mode.

> 中文:设 `ANTHROPIC_PASSTHROUGH=1` 时 relay 把 Messages 原样转给上游原生端点,Claude Code 才能看到 prompt caching 的 `cacheReadInputTokens`(同一会话 id 复用才会命中,所以 relay 转发 Claude Code 自己的会话 id,或退回 `RELAY_SESSION_ID`)。两点注意:Claude Code 显示的 `cost` 是它按内置价位估的,不等于上游账单;透传模式下只有支持 Anthropic 协议的模型可用。

### Codex

Either point Codex straight at the upstream (OpenAI `chat` wire — no relay needed):

```toml
# ~/.codex/config.toml
[model_providers.opencode]
name = "OpenCode Go"
base_url = "https://opencode.ai/zen/go/v1"
env_key = "OPENCODE_GO_API_KEY"
wire_api = "chat"
```

```bash
export OPENCODE_GO_API_KEY="..."
codex --provider opencode --model deepseek-v4-flash
```

Or route it through the relay for unified auth (works with both `wire_api = "chat"` and `wire_api = "responses"`):

```toml
# ~/.codex/config.toml
[model_providers.opencode-relay]
name = "OpenCode Go (relay)"
base_url = "http://your-server:8787/v1"
env_key = "RELAY_TOKEN"
wire_api = "chat"
```

## Deployment (systemd)

See [`examples/opencode-go-relay.service`](examples/opencode-go-relay.service) for a ready-to-adapt unit file. Use `EnvironmentFile=` to keep keys out of the unit itself.

For an Alibaba Cloud (mainland ECS) walkthrough — user-level Python via `uv`,
nginx HTTPS on a custom port with a path prefix, per-request key mode, security
group and ICP-filing notes — see [`docs/aliyun-deploy.md`](docs/aliyun-deploy.md).

## Security notes

- `OPENCODE_GO_API_KEY` is your paid subscription key — keep it **on the server only**. Clients talk to the relay, never to the upstream.
- Always set `RELAY_TOKEN`; the relay warns loudly on startup if it's missing.
- The relay is plain HTTP — terminate TLS in front of it (Caddy / nginx / Traefik) if it's exposed beyond a trusted network.

## Tests

`test_relay.py` spins up a mock upstream and validates every path (auth, models, Anthropic non-stream/stream in both translation and passthrough modes, Responses non-stream/stream, chat passthrough, `count_tokens`, session-id priority, error passthrough) with no network access:

```bash
python3 test_relay.py
```

## License

[MIT](LICENSE)
