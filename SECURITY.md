# Reporting a security issue

Please use [GitHub private vulnerability reporting](https://github.com/gadmin2151/ambergate/security/advisories/new). Do not disclose exploitable vulnerabilities, credentials, agent tokens or private configurations in public issues or discussions.

Include the affected version/image digest, component, prerequisites, impact and a minimal reproduction using disposable data. Reports in English or Russian are welcome. Identify whether you reproduced the issue on the current release or `main`; older versions may require an update before a fix can be evaluated. There is no guaranteed response time.

Test only systems and data you own or have permission to assess. Allow time to investigate and coordinate disclosure privately.

## Deployment context

- Keep the control panel on a trusted network or behind a protected HTTPS proxy. Managed domain SSL secures application traffic; the separate admin listener remains HTTP on 8083.
- Docker socket access is privileged even with a read-only bind mount. Extended agent namespace access is explicitly opt-in and intended for trusted Linux hosts.
- Use HTTPS for agent connections. The optional confirmed HTTP mode is unencrypted.
- Back up `/data` privately: it contains credentials, agent state and any certificate private keys.

These deployment assumptions do not prevent reporting a vulnerability. Explain the trust boundary and conditions involved. See the [agent guide](docs/agents.md) and [SSL guide](docs/ssl.md) for configuration details.

## Сообщить об уязвимости

Используйте [приватный отчёт GitHub](https://github.com/gadmin2151/ambergate/security/advisories/new). Не публикуйте уязвимости, пароли, токены и приватные конфигурации в Issues или Discussions. Укажите версию/ digest образа, затронутый компонент, условия, последствия и минимальный пример с тестовыми данными. Русский и английский поддерживаются. Гарантированного срока ответа нет; тестируйте только системы, на которые у вас есть разрешение.
