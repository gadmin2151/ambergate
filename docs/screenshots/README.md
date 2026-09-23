# Interface screenshots / Скриншоты интерфейса

[English README](../../README.md) · [Русский README](../../README.ru.md)

Captured from AmberGate (dashboard/rewriting: revision `bc51831`; domains/routes/SSL: the managed TLS update) on 23 September 2026 using Chromium. These are actual UI screenshots from an isolated local instance, not design mockups. Both English and Russian captures are included.

The demo uses `example.com`, `api.example.com` and `status.example.com`, with disposable loopback HTTP backends. Dashboard values come from generated HTTP traffic through real Nginx, including cache hits; they are **demonstration data, not performance benchmarks**. Screenshots contain no production domains, credentials, tokens or agent installation secrets.

| Files | Screen |
|:--|:--|
| `dashboard.en.png`, `dashboard.ru.png` | Live SSE dashboard with demo traffic |
| `domains.en.png`, `domains.ru.png` | Domain overview tiles |
| `routes.en.png`, `routes.ru.png` | Selected domain, routes and SSL status |
| `ssl.en.png`, `ssl.ru.png` | Optional certificate issuance and renewal settings |
| `rewriting.en.png`, `rewriting.ru.png` | Route response rewriting and a generic `{prefix}` substitution |

To refresh the images, start an isolated instance with disposable credentials and example domains. Generate test requests through its Nginx listener, sign in, and capture each language at a desktop viewport (1440 × 1100 or 1480 × 1040) with device scale 1. For the rewriting screen, open the route, expand additional HTML substitutions and capture the modal. Review every image for private data before committing.

---

Скриншоты сняты в Chromium с настоящего интерфейса (`bc51831` для dashboard/переписывания, обновление SSL для доменов/маршрутов/SSL) на отдельном локальном стенде, 23 сентября 2026 года. Используются примерные домены и временные HTTP-приложения. Показатели получены из тестовых запросов через Nginx: это **демонстрация интерфейса, а не измерение производительности**. Производственные адреса, пароли и токены не используются.
