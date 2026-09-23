# Changelog

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
