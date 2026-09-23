# AmberGate Agent

[Русский](agents.ru.md) · [Back to README](../README.md)

Run one **Docker container per remote machine**, with access to its Docker socket and private application addresses. The agent discovers containers and `ambergate.route` labels, then opens outbound HTTPS/WSS connections to central AmberGate. Applications and the agent need **no published ports**.

```mermaid
flowchart LR
    Browser[Application client] --> Edge[Your TLS proxy]
    Edge --> Nginx[Central AmberGate / Nginx]
    Agent[AmberGate Agent] -->|Outbound HTTPS / WSS| Control[Central AmberGate :8083]
    Nginx -->|Local upstream| Control
    Control <-->|Traffic over the established tunnel| Agent
    Agent --> A[Private container A]
    Agent --> B[Private container B]
```

Nginx still controls hosts, paths, balancing, caching and limits. The agent streams upstream HTTP traffic over the tunnel, including uploads, SSE and application WebSocket upgrades. It is not a general-purpose network VPN or public TCP forwarder.

## Quick setup: copy one command

1. Open **General settings** and save the address of central AmberGate **once**. A domain such as `ambergate.exemple.com` becomes `https://ambergate.exemple.com`. Your TLS proxy must forward that address to the central **admin listener on port 8083**, not the application listener on port 80.
2. Open **Agents → Add agent**, give the machine a name and create it.
3. Keep **Host network · local bridge networks (Linux)** selected; no network needs creating or naming. The **Docker run** tab shows a complete **single-line command**, with your saved address and the new agent token already inserted. Click **Copy command** and run it on the Docker machine.
4. Keep the wizard open: **Agent connected** appears automatically through SSE when the agent reports successfully. The **Docker Compose** tab offers a ready-to-run file and `docker compose -f compose.agent.yaml up -d` instead.
5. Configure routes manually using the container picker below, or add route labels to your applications. For labels, select **Preview** or **Apply automatically** in the automation card. The same label policy controls local Docker and all agents; the default is **Off**.

Example command (the wizard fills in your real address and token):

```bash
docker run -d --pull always --name ambergate-agent --restart unless-stopped --init --read-only --tmpfs /tmp:size=16m,mode=1777 --network host -e AMBERGATE_DOCKER_CONTAINER=ambergate-agent --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly -e AMBERGATE_SERVER_URL=https://ambergate.exemple.com -e AMBERGATE_AGENT_TOKEN='<AGENT_TOKEN>' ghcr.io/gadmin2151/ambergate-agent:latest
```

The default is **`--network host`** (Compose: **`network_mode: host`**). On native Linux Docker Engine, the agent reaches private container IPs across local bridge networks, including networks created by separate Compose projects. There is no need to create `ambergate-apps`, move your applications or publish their ports. The agent opens no inbound listener and never changes Docker networks. Both manual container selection and labels use this access; Docker IP changes are picked up on the next report.

For Docker Desktop, rootless Docker or access restricted to a specific network, choose **Specific Docker network** in the wizard and enter an existing name such as `my-app_default`. With the repository Compose file, set `AMBERGATE_AGENT_NETWORK=my-app_default`. Attach additional networks with `docker network connect <network> ambergate-agent` if required. An `internal: true` network needs an additional network with outbound connectivity to central AmberGate.

Host mode uses private IPs, not Docker DNS. Automatic reachability targets local **bridge** networks on native, rootful Linux Docker Engine; macvlan, ipvlan, overlay, containers in `none`/`host` mode and custom firewall rules are outside this guarantee. Host mode shares the host network namespace. [Docker host networking](https://docs.docker.com/engine/network/drivers/host/) · [Bridge networking](https://docs.docker.com/engine/network/drivers/bridge/).

Tokens are shown only in the installation wizard. The command and Compose file contain a secret: keep your copy private and do not commit it. For a downloaded file, `chmod 600 compose.agent.yaml` restricts access. If you close the wizard without saving a token, use **New token**; this revokes the previous token. To rotate an existing Docker-run installation, remove its old `ambergate-agent` container before running the new command; for Compose, replace the file and run `up -d`.

### Plain IP addresses and HTTP confirmation

You can enter just **`10.0.0.10`** in General settings. The panel proposes **`http://10.0.0.10:8083`** and shows an explicit warning: **agent tokens and application traffic will be sent without encryption**. Nothing is saved unless you confirm **I understand. Allow HTTP**. Cancelling retains the previous settings.

You may also specify `10.0.0.10:9000` or an explicit HTTP URL. After confirmation, generated commands and Compose files include `AMBERGATE_AGENT_ALLOW_HTTP=true`. Confirmation is tied to the saved address: changing to another HTTP address requires a new confirmation. HTTPS commands never contain this override. An explicit `https://IP` remains supported with normal certificate verification; a plain IP never silently becomes `https://IP`.

Changing the shared address affects new installation commands, not agents that are already running. Update their command or Compose file on their machines. General settings are stored locally in `/data/settings.json`, independently of Nginx drafts and routes.

Alternatively, use [compose.agent.yaml](../compose.agent.yaml) with a private `.env` file:

```dotenv
AMBERGATE_SERVER_URL=https://ambergate.exemple.com
AMBERGATE_AGENT_TOKEN=REPLACE_WITH_YOUR_AGENT_TOKEN
AMBERGATE_AGENT_NETWORK=host
AMBERGATE_AGENT_ALLOW_HTTP=false
```

Then start it with one command:

```bash
docker compose -f compose.agent.yaml up -d
```

The image is `ghcr.io/gadmin2151/ambergate-agent:latest` for amd64 and arm64. Agent and central images are built from the same revision; prefer matching versions.

## Extended namespace access (Linux)

In the installation wizard, choose **Extended access · namespace (Linux)**. The generated Docker command or Compose file adds `--pid host`, `SYS_ADMIN`, `SYS_PTRACE` and `AMBERGATE_AGENT_NAMESPACE=true`. The standalone [compose.agent.namespace.yaml](../compose.agent.namespace.yaml) works with the same private `.env` as the normal agent:

```bash
docker compose -f compose.agent.namespace.yaml up -d
```

The agent discovers all running containers from its local Docker daemon. When a selected route needs a connection, a short-lived helper enters that container's network namespace, opens its private application port, returns the connected socket to the agent and exits. Even `127.0.0.1` listeners and `network=none` applications can be reached. The agent remains one container, the outbound TLS tunnel stays in its original network, and application networks, images and ports are not modified.

This explicitly grants broad namespace access and is intended for trusted, native rootful Linux Docker hosts. Full `--privileged` and disabling seccomp are not required by the generated installer. Host security policies can still restrict namespace entry. Startup checks fail clearly if access is missing. Only locally discovered running container IDs are accepted; the center cannot supply a PID, namespace path or arbitrary destination address. Stopped/recreated containers are rechecked before opening a connection; the agent cannot select itself.

Update central AmberGate first, then recreate the agent with the new command. Existing host/shared-network agents keep working. Restarting an old container alone does not add the new capabilities. Labels and manual container selection both support this mode; private HTTP ports still need to be specified.

## Select remote containers in a route

Open **Routes → Add/edit route → Choose from Docker** and change **Container source** from **Local Docker** to your agent. Select running containers, choose their private HTTP ports (or enter a port manually), then **Add selected → Save route → Apply**. Labels, local Docker socket access on central AmberGate and published application ports are not required.

To balance several machines, add containers from one agent, reopen the picker and choose another agent. Leave **Replace current server list** unchecked to keep existing targets. Local servers and agent targets can share a route; weights and backup targets work normally.

The route displays the agent, container and application port. Traffic uses a persistent loopback mapping on central AmberGate and the agent's outbound tunnel. Container recreation with the same name updates its destination on the next report; stopped, removed or unreachable containers fail closed and Nginx can try another target. These manually selected routes remain under your control, including when label automation is **Off**. Their selection takes effect in Nginx only after applying the route.

**Existing agents need an image update for manual selection.** The picker detects older agents and explains this instead of generating an unusable route. For Compose, run `docker compose -f compose.agent.yaml pull` followed by `docker compose -f compose.agent.yaml up -d`. For Docker run, generate a **New token** in the agent card and recreate the old agent with the new installation command. Update central AmberGate first.

Manual registrations are stored in `/data/agent-manual.json`, with stable ports in `/data/agent-ports.json`. Back up the entire `/data` directory with routes; route JSON alone cannot transfer these host-local tunnel mappings to another installation. Removing a route does not recycle its port, so history cannot silently point to another container. The shared limit is 1024 retained tunnel mappings.

## Compact labels, private container ports

```yaml
services:
  backend:
    image: your-backend:latest
    networks: [applications]
    labels:
      ambergate.route: "host=example.com; path=/api; port=3000; strip=true"
    # No ports: block required. 3000 is the application's private HTTP port.

networks:
  applications: {}
```

Run replicas with the **same host, path and route options** on several machines. Central AmberGate combines their tunnel targets in a single Nginx upstream. `weight`, `backup` and the supported balancing algorithms work as with local labels.

To extend a manually configured route, set its **Docker group** to `app-api` and use:

```yaml
labels:
  ambergate.route: "group=app-api; port=3000; weight=2"
```

[All label options and examples](docker-labels.md#label-reference) also apply, except `via=host`: remote agents use private container endpoints. Omit `via`, or set `via=network`. Host mode reaches local bridge networks automatically; extended mode opens connections inside the target container's network namespace. In specific-network mode, connect the agent to every application network; the agent never joins networks or publishes ports by itself.

[Complete application + agent Compose example](../examples/agent.compose.yaml).

## External TLS proxy

Use your existing TLS reverse proxy. It must allow WebSocket upgrades and preserve the `Authorization` header for `/api/agents/`. For Nginx, inside your **existing TLS server**:

```nginx
location / {
    proxy_pass http://10.0.0.10:8083; # Central AmberGate admin listener
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 90s;
    proxy_send_timeout 90s;
    proxy_buffering off; # Also keeps the panel's SSE live
}
```

Change `10.0.0.10` to your central server. Configure certificates in that proxy as usual. HTTPS is the default. URL path prefixes, redirecting login gateways and HTTP redirects are not supported for agents. If the panel is behind SSO, let `/api/agents/connect`, `/api/agents/tunnel/*` and `/api/agents/report` use AmberGate's own bearer-token authentication; retain your existing protection for the rest of the panel. Agent tokens never authenticate admin API calls.

For a private CA, mount its PEM bundle in the agent and set `AMBERGATE_AGENT_CA_FILE=/run/secrets/ca.pem`. Certificate and hostname verification stay enabled. There is no insecure TLS switch.

## State, revocation and failures

- Inventory is reported every 5 seconds. The panel receives agent status through its existing **SSE stream**. Container details show the latest received inventory when opened.
- Running, labelled containers with a reachable endpoint (host network, shared network or namespace access) become targets. Application responses are monitored by Nginx; Docker health-check status does not independently remove a running container. Invalid labels, an incomplete inventory or loss of Docker access preserve that agent's last valid report until its lease expires.
- In **Auto** mode, expired agents are removed on the next reconciliation (up to 5 additional seconds). Other agents and manually configured targets remain. An empty managed route returns **503**.
- **Preview** proposes configuration changes; **Off** leaves configuration unchanged. Tunnel disconnection still fails upstream connections immediately, allowing Nginx to try a remaining healthy target.
- Disabling, deleting or rotating a token immediately disconnects that agent's tunnel and rejects the old token. Rotation requires updating and restarting the remote agent.
- Agents reconnect automatically after a central restart. Stable local upstream mappings survive restarts and container recreation by name. They are never reassigned to another application, so old configuration history cannot accidentally route to an unrelated target.
- Central stores token **hashes**, inventory, source ownership and port mappings under `/data`. Back up the complete data directory. Agent tokens must be saved separately on their remote machines.

## Settings and limits

| Agent environment variable | Default / purpose |
|---|---|
| `AMBERGATE_SERVER_URL` | Saved central admin origin; HTTPS by default |
| `AMBERGATE_AGENT_TOKEN` | Per-agent token generated in the panel |
| `AMBERGATE_AGENT_TOKEN_FILE` | Optional mounted secret file; takes precedence over the environment token |
| `AMBERGATE_DOCKER_SOCKET` | `/var/run/docker.sock` |
| `AMBERGATE_DOCKER_CONTAINER` | `ambergate-agent` in generated installers; own container name/ID. Update this variable if you rename the container |
| `AMBERGATE_AGENT_NAMESPACE` | `false`; opt-in namespace connector with the required host permissions |
| `AMBERGATE_AGENT_CA_FILE` | Optional PEM CA bundle |
| `AMBERGATE_AGENT_INTERVAL` | 5 seconds; allowed 2–30, keep below the configured lease timeout |
| `AMBERGATE_AGENT_ALLOW_HTTP` | `false`; enabled by generated installers only after explicit HTTP risk confirmation |

Initial bounds: **32 agents**, **32 simultaneous upstream TCP streams per agent**, **128 total tunnel streams**, **500 containers per report**, **1 MiB per report**, and **1024 retained tunnel target mappings**. Normal route limits also apply (32 combined targets per route). Idle upstream keepalives expire after 5 seconds for routes using remote agents. Each active stream uses an outbound WebSocket; long-lived SSE and WebSocket application connections count toward the stream limit.

The agent reads only Docker metadata needed for inventory and routing; environment variables, mounts and unrelated labels are not reported. Docker socket access still grants privileged host access at the API level: a `:ro` bind mount does not enforce read-only Docker operations. Run agents only on trusted machines and keep their tokens private.
