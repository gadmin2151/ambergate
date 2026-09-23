# Contributing to AmberGate

Thanks for helping improve the gateway. Bug reports and pull requests are welcome in **English or Russian**.

## Run locally

```bash
docker compose up -d --build
docker compose logs ambergate
```

The control panel is at `http://127.0.0.1:8083`; the initial password appears once in the container logs. Use disposable application containers and example domains when testing.

## Before a pull request

```bash
bash -n first-start.sh
node --check ambergate/static/i18n.js
node --check ambergate/static/app.js
node --check ambergate/static/dashboard.js
node --check ambergate/static/docker.js
node --check ambergate/static/agents.js
node --check ambergate/static/general.js
node --test tests/*.test.js
python3 -m unittest discover -v
docker compose config --quiet
```

Integration tests need Nginx 1.28+ and are skipped without it. To run against the image:

```bash
docker compose build
docker run --rm --entrypoint python3 -v "$PWD/tests:/app/tests:ro" \
  local/ambergate:latest -m unittest discover -v
```

CI additionally verifies real Docker discovery, published host ports, load balancing and DNS changes after container recreation. It checks installation inside an isolated Debian container. `tests/agent_docker_smoke.py` additionally runs private application containers and an agent through a verified TLS proxy, checking balancing, uploads, central restart, lease expiration and revocation. Build `ambergate:ci` and `ambergate-agent:ci` (`Dockerfile.agent`) before running it.

## Project layout

| Path | Purpose |
|:--|:--|
| `ambergate/config.py`, `ambergate/nginx.py` | Validated model and Nginx generation |
| `ambergate/storage.py` | Drafts, active versions, apply and rollback |
| `ambergate/server.py`, `ambergate/auth.py` | Admin API, static files and authentication |
| `ambergate/metrics.py`, `ambergate/events.py` | Metrics collection and SSE delivery |
| `ambergate/agent.py`, `ambergate/agents.py`, `ambergate/tunnel*.py` | Remote discovery, agent identities and outbound traffic tunnels |
| `ambergate/docker.py` | Docker discovery and reachable targets |
| `ambergate/static/` | Dependency-free panel, styles and RU/EN catalog |
| `first-start.sh` | Debian/Ubuntu first-start installer |
| `tests/` | Unit, Nginx integration, browser logic and Docker smoke tests |

## UI translations

Use `t('Russian source phrase')` for authored labels and `ui` tagged templates for HTML containing authored text. The tag translates only static template segments; interpolated route names, addresses and other user data stay unchanged. Add English entries to `ambergate/static/i18n.js`. Translate dynamic server diagnostics with `translateError()` at display time. Format dates and numbers with `locale()`.

Check both languages, keyboard navigation, mobile layout, unsaved forms and SSE reconnection. Do not translate configuration values or introduce external fonts, scripts or analytics.

## Reporting an issue

Include steps, expected and actual behavior, image tag/commit and relevant sanitized logs. Replace private domains and addresses with examples. Never post passwords, cookies, tokens, `/data/admin.json` or an unredacted backup. For a security concern, use the repository's private vulnerability reporting option if available instead of publishing exploit details in an issue.

Keep changes focused. Describe the user-visible result and relevant validation in the pull request.
