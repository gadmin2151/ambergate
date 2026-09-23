# Keep an application under a route prefix

[Русский](response-rewriting.ru.md) · [README](../README.md)

An application at `/api5` may return `Location: /login`, a cookie with `Path=/`, or HTML with links to `/public/...`. Without response rewriting, the browser leaves `/api5` and requests those paths from the domain root.

In the route editor, enable **Keep the base path in responses**, then save and apply. It enables prefix stripping for requests: the application receives `/login` while the browser uses `/api5/login`. This works with direct upstreams and agent targets. No application configuration changes are made.

AmberGate generates standard Nginx rules to:

- Redirect the exact `/api5` entry to `/api5/`, preserving query parameters and HTTP methods with status 308.
- Prefix root-relative `Location`/`Refresh` redirects and absolute redirects to the route's domain or configured upstream addresses. External destinations and already-prefixed paths are retained.
- Scope cookie paths under `/api5`.
- Prefix quoted, root-relative HTML `href`, `src`, `action`, `formaction` and `poster` attributes, including `<base href="/">`. Protocol-relative external URLs are retained.

**Additional HTML substitutions** provides up to 16 literal search/replacement rules. These are ordinary route configuration; AmberGate has no application-specific branches or presets. `{prefix}` in a replacement expands to the route path. For example, an application's embedded HTML configuration can use:

| Find | Replace with |
|---|---|
| `"basePath":""` | `"basePath":"{prefix}"` |

Rules are limited to 1024 characters per field, a single line and no `$` characters. Searches are case-insensitive, as in Nginx `sub_filter`. Changes stay in the local draft until applied and are included in configuration history.

Rewriting is opt-in for non-root routes with prefix stripping. It applies to HTML, not JavaScript bundles, JSON API bodies or binary responses. Upstream compression is disabled for these routes so HTML can be processed. Arbitrary client-side URL generation, CSS URLs, `srcset`, CSP, OAuth origins and application-specific boot data may still need explicit route substitutions or a separate hostname; this feature cannot infer every application's URL semantics. Incoming authorization, cookies and WebSocket support retain normal gateway behavior. Do not enable it for signed APIs such as S3.

HTTPS at the external TLS proxy is compatible: AmberGate handles HTTP upstream responses before the external proxy encrypts them for the browser.
