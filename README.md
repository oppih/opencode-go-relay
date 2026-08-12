# OpenCode Go Relay

A zero-dependency protocol bridge that lets **Claude Code** (Anthropic Messages) and **Codex** (OpenAI Responses) share the OpenAI-compatible models in your [OpenCode Go](https://opencode.ai) subscription — default model `deepseek-v4-flash`. Pure Python standard library, single file, no third-party deps.

> 中文简介:一个零依赖的协议中转站,让 Claude Code(Anthropic Messages 协议)和 Codex(OpenAI Responses 协议)共用 OpenCode Go 订阅里的 OpenAI 兼容模型。纯 Python 标准库,单文件,无第三方依赖。

## Why?

OpenCode Go exposes an OpenAI-compatible endpoint (`https://opencode.ai/zen/go/v1`), but the models behind it (e.g. DeepSeek V4 Flash) only speak `chat/completions`:

- **Claude Code** only speaks the Anthropic Messages protocol → needs a bridge to translate Messages ⇄ chat/completions (including streaming SSE and tool calls).
- **Codex** can point directly at the upstream with `wire_api = "chat"` (no relay needed), but routing it through the relay gives you a single auth token, a single model alias, and one place to deploy.

## Features

- **`POST /v1/messages`** (Anthropic) ⇄ upstream `chat/completions`
  - system blocks, tool use / tool results, images (URL + base64), thinking blocks
  - `tool_choice` mapping (`any` → `required`, named tool, etc.)
  - full SSE streaming translation: `message_start`, `content_block_delta`, `input_json_delta`, `message_delta`, `message_stop`
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
| `UPSTREAM_BASE` | `https://opencode.ai/zen/go/v1` | Upstream OpenAI-compatible base URL. |
| `HOST` / `PORT` | `0.0.0.0` / `8787` | Listen address. |
| `STREAM_OPTIONS` | `1` | Send `stream_options.include_usage` upstream; set `0` if the upstream rejects it. |
| `REQUEST_TIMEOUT` | `600` | Upstream request timeout (seconds). |
| `UPSTREAM_UA` | *(browser UA)* | User-Agent sent upstream; some edges (e.g. Cloudflare) reject urllib's default. |
| `MAX_CONNECTIONS` | `64` | Max concurrent connections; excess connections wait (backpressure). |
| `CLIENT_TIMEOUT` | `60` | Per-connection read timeout in seconds; idle connections (slowloris) are dropped. |

### Endpoints

| Path | Protocol | Translation |
|---|---|---|
| `POST /v1/messages` | Anthropic | → `chat/completions` |
| `POST /v1/responses` | Responses | → `chat/completions` |
| `POST /v1/chat/completions` | OpenAI | passthrough (model rewritten) |
| `GET /v1/models` | OpenAI | model list |
| `GET /healthz` | — | health check (no auth) |

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

`test_relay.py` spins up a mock upstream and validates all six paths (auth, models, Anthropic non-stream/stream, Responses non-stream/stream, chat passthrough) with no network access:

```bash
python3 test_relay.py
```

## License

[MIT](LICENSE)
