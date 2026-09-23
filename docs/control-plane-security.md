# Protect the AmberGate control panel

AmberGate v0.2.0 bounds incoming admin requests and separates pending connections from authenticated admin, agent-report and tunnel capacity. Headers must finish within 10 seconds, and JSON request bodies within 30 seconds. These are total read deadlines. Authenticated SSE and WebSocket connections are not cut off by these deadlines.

The admin listener on port **8083 remains HTTP**. Keep it private and use a protected HTTPS reverse proxy for remote access. Domain certificates on the application gateway do not encrypt this separate listener.

## Client identity behind a proxy

On the central gateway, configure only the proxy addresses actually seen as TCP peers:

```dotenv
AMBERGATE_ADMIN_TRUSTED_PROXIES=10.0.0.5/32
AMBERGATE_SECURE_COOKIE=true
AMBERGATE_AGENT_TARGET_LIMIT=128
```

These variables are forwarded by the supplied gateway Compose files. For an existing installer-generated Compose file, add these variables to the gateway service’s `environment` mapping as well. Recreate the gateway after changing `.env`:

```bash
docker compose up -d
```

In the HTTPS proxy's admin location, overwrite the forwarding header:

```nginx
proxy_set_header X-Forwarded-For $remote_addr;
```

See the complete [agent-compatible proxy example](agents.md#external-tls-proxy). If there are multiple proxies, each hop must sanitize or append a verified peer address; list only those trusted hops. AmberGate walks the chain from the nearest proxy toward the client and stops at the first untrusted address. An empty allowlist ignores forwarded addresses. Duplicate, malformed or oversized headers fall back to the socket peer. Never trust `0.0.0.0/0` or a client network.

Login attempts use a per-client burst of 10, refilling one attempt every 30 seconds. Rate-limited responses include `Retry-After`. Password checks have a separate concurrency limit and do not block existing session checks. Users sharing the same actual NAT address still share an IP budget; use a private administrative access path if independent identities are required. The application routes' trusted-proxy settings are separate from this admin setting.

## Agent targets

The central gateway allows 128 persistent targets per agent by default, adjustable with `AMBERGATE_AGENT_TARGET_LIMIT` from 1 to 512. The global limit is 1024. Reports and previews allocate no new listeners. Manual selection or applying label changes reserves the required targets in a batch; rejected batches do not consume capacity.

Existing mappings above a reduced quota continue working. Startup removes only reservations absent from the draft, retained configuration history and manual selections. Keep a private backup of all `/data`; do not delete `agent-ports.json` to clear a quota. Quota diagnostics appear in agent status or the label preview. Remove obsolete routes and retained references deliberately before expecting their reservations to be reclaimed.
