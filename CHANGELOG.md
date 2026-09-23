# Changelog

## 0.2.0 — 2026-09-23

### Control-plane reliability and security

- Absolute deadlines for reading admin HTTP headers and request bodies, bounded pending connections per source, and separate capacity for authenticated admin requests, agent reports and tunnel streams. SSE and WebSocket streams keep their own lifetime rules.
- Container inventories and label previews no longer reserve tunnel ports. Accepted targets are allocated as a batch with rollback on failure and a default quota of 128 persistent targets per agent. Existing route addresses and retained history remain stable.
- Explicit trusted admin proxies, independent client login budgets, accurate retry hints and bounded password verification that does not hold the session lock. Forwarded client IPs from untrusted connections are ignored.
- Regression coverage for slow input, proxy attribution, resource quotas, upgrade state and long-lived connections.

### Upgrade notes

Back up `/data`, then update the gateway and agent images to `0.2.0`. The agent wire protocol remains compatible. Central startup removes reservations absent from the saved draft, retained revisions and manual selections; referenced addresses remain unchanged. Existing targets above the new quota still work.

Set `AMBERGATE_ADMIN_TRUSTED_PROXIES` on the gateway when using a reverse proxy, and have that proxy overwrite `X-Forwarded-For` with the actual client address. Empty trust lists ignore forwarded IPs. Tune `AMBERGATE_AGENT_TARGET_LIMIT` on the gateway if needed (1–512; default 128; total 1024). The supplied Compose files pass both settings from `.env`. These changes do not enable TLS on admin port 8083; keep it private or behind a protected HTTPS proxy.

[English setup](docs/control-plane-security.md) · [Настройка на русском](docs/control-plane-security.ru.md)

## 0.1.0 — 2026-09-23

First public versioned release of AmberGate. This is an early project; test upgrades against your applications and keep a private backup of `/data`.

### Routing and visibility

- Nginx and a RU/EN web panel in one Docker container, with local configuration storage.
- Domain overview tiles and a dedicated workspace for each domain's routes and SSL settings.
- Multi-domain routing, prefix stripping, weighted/backup balancing, caching, rate limits and basic HTTP protections.
- Live SSE dashboard, configuration drafts, checked reloads, rollback and revision history.
- Optional response rewriting for redirects, cookie paths and HTML, with additional literal HTML substitutions.

### Docker and remote machines

- Local Docker discovery through a mounted socket and published-host-port fallback.
- One-line `ambergate.route` labels for route creation and load-balancer membership.
- Containerized agents with outbound HTTPS/WSS tunnels, manual remote-container selection and balancing across machines.
- Linux host-network mode and optional extended namespace access, including loopback-only applications.

### Optional SSL

- Per-domain Let’s Encrypt HTTP-01 issuance, disabled by default.
- Automatic renewal starting five days before expiry by default; configurable from 1 to 30 days.
- Optional HTTP-to-HTTPS redirects, live certificate state, persistent retry backoff and verified certificate reloads.
- Real Certbot/Pebble issuance and renewal checks in CI.

### Installation and community

- First-start installer, Docker Compose examples and amd64/arm64 gateway and agent images on GHCR.
- Bilingual documentation, real UI screenshots and service connection diagrams.
- MIT license, contribution guide, issue forms and private vulnerability reporting.

### Upgrade notes

Existing routes keep SSL disabled. To enable application HTTPS, publish TCP 443 and make the HTTP-01 path reachable on public TCP 80. Saved drafts do not request certificates. Admin and agent control traffic still use the separate 8083 listener; use an external HTTPS proxy for them.

Response rewriting does not automatically cover JavaScript, JSON or every application's URL handling. DNS-01, wildcard/IP certificates, HTTPS upstreams, gRPC and arbitrary TCP proxying are outside this release.

[Installation](README.md#quick-start) · [Русский README](README.ru.md) · [SSL setup](docs/ssl.md)
