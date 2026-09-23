# Docker labels

[Русский](docker-labels.ru.md) · [README](../README.md)

Describe a route in **one label**. AmberGate discovers running containers, creates the domain and route, and adds matching replicas to the same load balancer:

```yaml
labels: { ambergate.route: "host=example.com; path=/api; port=3000; strip=true; balance=least_conn" }
```

For local containers, mount the Docker socket using `compose.docker.yaml` and connect it in **Docker**; for remote machines, connect an [agent](agents.md). Then choose a mode under **Routes from Docker labels**:

- **Off** (default): no automatic changes; existing routes are retained.
- **Preview changes**: inspect discovered routes and targets, then click **Apply discovered changes**.
- **Apply automatically**: reconcile running containers every **5 seconds**, including starts, stops, removal and replica changes. The panel receives status through SSE.

Every changed configuration passes `nginx -t` and a verified graceful reload. Failed changes roll back. No reload occurs when nothing changed. Active configuration and saved drafts are reconciled separately: automatic discovery never promotes unrelated draft edits. Concurrent editor saves use revision checks; refresh an outdated editor before saving again.

## Join an existing route

In the route editor, open **Servers**, enter **Docker group** `app-api`, then save and apply. Keep manual upstreams or remove them to use only discovered members. Each matching container joins that route:

```yaml
labels: { ambergate.route: "group=app-api; port=3000; weight=2" }
```

Groups use the route's existing balancing, cache and protection settings. `host`/`path` labels never overwrite a manual route at the same domain and path; use a group instead. A group without a matching route is reported in the panel. A group may be used by multiple routes.

## Label reference

Use semicolon-separated `key=value` fields. Unknown keys, duplicate fields and invalid values are rejected; booleans are `true` or `false`.

| Key | Meaning / default |
|:--|:--|
| `host` | Domain to create; required unless using `group`; one domain per label |
| `path` | `/` by default; e.g. `/api` or `/v1/api`, without a trailing slash |
| `port` | **Required container HTTP port**, 1–65535; no `EXPOSE` required in a shared network |
| `group` | Join existing routes by group; 1–64 letters, digits, `_`, `-`; replaces `host`/`path` |
| `via` | `network` (default, shared Docker network) or `host` (published port via Docker host IP) |
| `balance` | `round_robin` (default), `least_conn`, `ip_hash` |
| `strip` | Remove the path prefix; default `false` |
| `cache` | Cache public GET/HEAD responses; default `false`; existing cookie/auth/signed-request protections still apply |
| `ttl` | Cache lifetime in seconds, 1–604800; default `60` |
| `websocket` | Enable Upgrade forwarding; default `true` |
| `timeout` | Upstream read/send timeout in seconds, 1–3600; default `60` |
| `body` | Request-body limit in MiB, 0–102400; `0` inherits the global limit |
| `rate` | Additional per-route requests/second per source IP, 0–100000; `0` leaves only the global limit |
| `burst` | Burst for the route rate limit, 0–100000; default `0` |
| `weight` | This container's upstream weight, 1–1000; default `1` |
| `backup` | Reserve upstream, default `false`; incompatible with `ip_hash` |

`group` labels accept only `group`, `port`, `via`, `weight`, `backup`. Containers sharing `host` + `path` must agree on route options; their ports, access methods and weights may differ. Global Nginx settings, trusted proxies and TLS are not configured through labels.

For local discovery across Docker networks, you can publish the application port and configure **Docker host IP** in the panel:

```yaml
ports: ["8001:80"]
labels: { ambergate.route: "host=storage.example.com; port=80; via=host" }
```

AmberGate maps container port `80` to host port `8001`. It respects explicit bind addresses; a localhost-only publication is unavailable from another container. `via=network` never silently falls back to a published port.

Multiple routes on one container use named labels:

```yaml
labels:
  ambergate.route.api: "host=example.com; path=/api; port=3000"
  ambergate.route.admin: "host=admin.example.com; port=3001"
```

**[Runnable Compose examples →](../examples/docker-labels.compose.yaml)** — frontend, scalable API, cache, multiple domains, groups, backups and another network.

Discovery uses running containers; it does not perform active HTTP readiness checks or filter Docker HEALTHCHECK results. Stopped/removed containers leave the discovered upstream set. If a route has no remaining manual or discovered targets, it stays present and responds **503**, so requests cannot fall through to another application. To permanently delete a label route, remove its label (and recreate the container), then delete the retained route in the panel. Label-owned route settings are read-only in the editor.

Invalid local labels or conflicting route options pause the update and retain the last valid configuration. Incomplete or unavailable local Docker retains its targets; remote inventory failures follow the agent lease policy. Limits: 16 route labels/container, 4096 characters/label, 32 total upstreams/route, 50 routes/domain and 100 domains. Inventories reaching 500 containers are treated as incomplete. All state is local: `/data/docker.json`, `/data/labels.json`, drafts and immutable configuration revisions. Disabling labels freezes existing routes. With remote agents configured, unavailable local Docker freezes only its local targets; agent leases continue to be reconciled. Only label containers you trust: anyone able to create labeled containers on a connected daemon can influence routing when automation is enabled.

## Containers through agents

The same labels work on remote machines through [AmberGate Agent](agents.md). Use the private container HTTP port; no published port is needed. Keep `via=network` or omit `via`: connectivity is selected when installing the agent. Host mode uses local bridge networks; namespace mode connects inside the selected container, including localhost listeners and `network=none`. Remote agents do not support `via=host`. Matching labels from multiple agents combine replicas into one load balancer.
