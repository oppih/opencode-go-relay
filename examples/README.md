# Examples

Ready-to-adapt config for the relay and its two clients. Nothing here contains secrets — replace
the placeholders.

| File | What it is |
|---|---|
| `.env.example` | Server-side environment (keys, port, and every optional switch). Copy to `/etc/opencode-go-relay.env`, `chmod 600`. |
| `opencode-go-relay.service` | systemd unit; reads the file above via `EnvironmentFile=`. |
| `claude-code-settings.json` | Claude Code client config: point it at the relay with `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`. |
| `codex-config.toml` | Codex client config: either straight at the upstream (`chat` wire) or through the relay for unified auth. |

## Two things worth knowing before you copy these

**Claude Code settings are strict JSON.** `claude-code-settings.json` is therefore written
without comments so it works regardless of whether your client tolerates JSONC. Merge the `env`
block into `~/.claude/settings.json` rather than replacing the file — that file usually holds your
own settings.

**Relay mode changes what the client sees.** With `ANTHROPIC_PASSTHROUGH=0` (the default) the relay
translates Messages to `chat/completions`: everything works, but prompt caching, thinking blocks and
native `tool_use` are lost in translation, so `cacheReadInputTokens` stays 0. With
`ANTHROPIC_PASSTHROUGH=1` the relay forwards to the upstream Anthropic-native endpoint and those
fields survive — but only models that speak the Anthropic protocol are usable in that mode
(others answer `400 ModelProtocolUnsupported`). See the README section *Anthropic passthrough*.

If you wrap the relay in a shell script, export the variables **before** the `exec` line: anything
after `exec python3 relay.py` never runs, and the relay quietly keeps its default mode.
