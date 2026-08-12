# 阿里云部署文档 (OpenCode Go Relay)

把 relay 部署到阿里云 ECS（Alibaba Cloud Linux 3 / al8），只暴露 HTTPS 入口，
按请求取 key 模式运行（服务器不保存任何 API key）。本部署不需要系统级 sudo
来运行 relay 本体，只有装 nginx/证书时需要 root。

> 本文档中的 `<YOUR_DOMAIN>`、`<SERVER_IP>`、`<SSH_USER>`、`<SSH_PORT>`、
> `<OPENCODE_GO_KEY>` 均为占位符，请替换为你自己的值。

## 架构

```
Claude Code (本机)
  │  ANTHROPIC_BASE_URL=https://<YOUR_DOMAIN>:8558/opencode-go/anthropic
  ▼
nginx :8558 ssl (公网唯一入口)
  │  /opencode-go/anthropic/  →  http://127.0.0.1:8787/
  ▼
relay.py :8787 (仅监听 127.0.0.1, 按请求取 key)
  │  把请求头里的 key 原样透传
  ▼
https://opencode.ai/zen/go/v1 (OpenCode Go)
```

每个请求必须携带客户端自己的 OpenCode Go key
(`Authorization: Bearer <OPENCODE_GO_KEY>` 或 `x-api-key: <OPENCODE_GO_KEY>`)，
relay 只做协议转换（Anthropic Messages / Responses → chat/completions）并透传 key。

## 前提

- 阿里云 ECS，公网 IP `<SERVER_IP>`，域名 `<YOUR_DOMAIN>` 已解析到该 IP
- 安全组放行 TCP `8558`（入方向）；`8787` 不需要对外
- 有 root 访问（用于安装/配置 nginx 和证书）

## 部署步骤

### 1. 创建专用用户（root 操作，可选但推荐）

```bash
useradd -m -s /bin/bash opencode-go
mkdir -p /home/opencode-go/.ssh
echo "你的公钥" > /home/opencode-go/.ssh/authorized_keys
chown -R opencode-go:opencode-go /home/opencode-go/.ssh
chmod 700 /home/opencode-go/.ssh && chmod 600 /home/opencode-go/.ssh/authorized_keys
```

### 2. 安装用户级 Python 3.12（不需要 sudo）

服务器自带 Python 通常低于 3.9，relay 要求 3.9+。用 uv 安装独立 Python：

```bash
ssh -p <SSH_PORT> opencode-go@<SERVER_IP>
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
sh /tmp/uv-install.sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12
uv python find 3.12   # 记下返回的路径, 后续启动脚本要用
```

### 3. 克隆仓库并验证

```bash
git clone https://github.com/<YOUR_GITHUB>/opencode-go-relay.git ~/opencode-go-relay
cd ~/opencode-go-relay
uv run --python 3.12 python test_relay.py    # 全部 PASS 再继续
```

### 4. 启动脚本 + 开机自启

`run.sh`（relay 只监听 `127.0.0.1`，按请求取 key 模式）：

```bash
#!/usr/bin/env bash
set -e
cd "$HOME/opencode-go-relay"
PY="<上一步 uv python find 输出的路径>"
LOG="$HOME/opencode-go-relay/relay.log"
PIDFILE="$HOME/opencode-go-relay/relay.pid"

start() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running (pid $(cat "$PIDFILE"))"; return 0
  fi
  nohup env HOST=127.0.0.1 "$PY" relay.py >>"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started pid $(cat "$PIDFILE")"
}
stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"; echo "stopped"
  else
    echo "not running"
  fi
}
case "${1:-start}" in
  start) start ;; stop) stop ;;
  restart) stop; sleep 1; start ;;
  *) echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
```

```bash
chmod +x ~/opencode-go-relay/run.sh
~/opencode-go-relay/run.sh start
curl -s http://127.0.0.1:8787/healthz        # {"ok": true}
curl -s http://127.0.0.1:8787/healthz >/dev/null && \
  (crontab -l 2>/dev/null | grep -v "opencode-go-relay/run.sh"; \
   echo "@reboot /home/opencode-go/opencode-go-relay/run.sh start") | crontab -
```

### 5. 证书（root 操作）

已有证书就跳过；没有则签发（需要 80 端口可访问，或 DNS-01 挑战）：

```bash
certbot certonly --nginx -d <YOUR_DOMAIN>
```

确认证书路径：
`/etc/letsencrypt/live/<YOUR_DOMAIN>/fullchain.pem` 与 `privkey.pem`。

### 6. nginx 反向代理（root 操作）

新建 `/etc/nginx/conf.d/opencode-go-relay.conf`：

```nginx
server {
    listen 8558 ssl http2;
    server_name <YOUR_DOMAIN>;

    ssl_certificate /etc/letsencrypt/live/<YOUR_DOMAIN>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<YOUR_DOMAIN>/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # https://<YOUR_DOMAIN>:8558/opencode-go/anthropic/v1/messages
    #   -> http://127.0.0.1:8787/v1/messages
    location /opencode-go/anthropic/ {
        proxy_pass http://127.0.0.1:8787/;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 660s;
        proxy_send_timeout 660s;
    }
}
```

```bash
nginx -t && systemctl reload nginx
curl -s https://127.0.0.1:8558/opencode-go/anthropic/healthz   # {"ok": true}
```

### 7. 安全组

在阿里云控制台放行 TCP `8558`（入方向）。`8787` 不要对外放行——
relay 只监听 `127.0.0.1`，即使放行也连不上，但保持不放行更干净。

### 8. 客户端 (Claude Code)

`~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://<YOUR_DOMAIN>:8558/opencode-go/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<OPENCODE_GO_KEY>",
    "ANTHROPIC_MODEL": "deepseek-v4-flash"
  }
}
```

或命令行：

```bash
ANTHROPIC_BASE_URL="https://<YOUR_DOMAIN>:8558/opencode-go/anthropic" \
ANTHROPIC_AUTH_TOKEN="<OPENCODE_GO_KEY>" \
ANTHROPIC_MODEL="deepseek-v4-flash" \
claude
```

## 运维

- 启停：`~/opencode-go-relay/run.sh {start|stop|restart}`
- 日志：`~/opencode-go-relay/relay.log`
- 健康检查：`curl -s https://<YOUR_DOMAIN>:8558/opencode-go/anthropic/healthz`
- 证书续期：`certbot renew` 已由 `certbot-renew.timer` 或 crontab 自动执行，
  续期后 `systemctl reload nginx`（post-hook 里配置）
- 升级：`cd ~/opencode-go-relay && git pull && run.sh restart`

## 合规与安全提示

- **ICP 备案**：大陆服务器上，未备案域名会被阿里云在 HTTP/HTTPS 访问时拦截
  （返回 "Non-compliance ICP Filing" 页面），请确保域名已完成备案。
- **个人自用**：不要公开分享服务地址；没有设置 `RELAY_TOKEN` 时，任何知道
  地址的人都可以用自己的 key 借用转发。
- **密钥隔离**：服务器不存储任何 OpenCode Go key；客户端 key 走 HTTPS 传输。
- **不收费、不公开注册**：多人/商业化使用前请先阅读 OpenCode Go 的服务条款。

## 排错

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 公网连不上 8558 | 安全组没放行 / 云防火墙 | 控制台放行 TCP 8558 |
| 域名访问返回备案拦截页 | 域名未 ICP 备案 | 完成备案或用已备案域名 |
| 请求返回 401 | 请求头没带 key | `Authorization: Bearer <key>` 或 `x-api-key: <key>` |
| relay 报 "missing API key" | 客户端没配置 `ANTHROPIC_AUTH_TOKEN` | 在 settings.json 或环境变量里配置 |
