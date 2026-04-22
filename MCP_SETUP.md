# SmartTeX MCP + OAuth (Claude Web)

## 1) Підняти сервіси

```bash
docker compose up -d --build db web mcp
```

Важливі змінні в `.env`:

```env
HOST_PROJECT_ROOT=/absolute/path/to/SmartTeX
OAUTH_ISSUER_URL=https://<your-web-domain>
OAUTH_INTROSPECTION_SECRET=<random-secret>
MCP_OAUTH_ENABLED=True
MCP_SERVER_PUBLIC_URL=https://<your-mcp-domain>
MCP_AUTH_SERVER_ISSUER_URL=https://<your-web-domain>
MCP_INTROSPECTION_URL=http://web:8000/oauth/introspect/
MCP_INTROSPECTION_SECRET=<same-random-secret>
```

Після зміни `.env` робіть recreate (не просто restart):

```bash
docker compose up -d --force-recreate web mcp
```

## 2) Публічні URL

ngrok запускаємо **локально** (не в Docker):

```bash
ngrok http 8000   # web (oauth issuer)
ngrok http 9000   # mcp
```

Підставте домени в `.env`:

- `OAUTH_ISSUER_URL=https://<web-ngrok-domain>`
- `MCP_SERVER_PUBLIC_URL=https://<mcp-ngrok-domain>`
- `MCP_AUTH_SERVER_ISSUER_URL=https://<web-ngrok-domain>`

Потім:

```bash
docker compose up -d --force-recreate web mcp
```

## 3) OAuth ендпоїнти

SmartTeX тепер має:

- `/.well-known/oauth-authorization-server`
- `/oauth/register/` (Dynamic Client Registration)
- `/oauth/authorize/`
- `/oauth/token/`
- `/oauth/introspect/`

## 4) Підключення в Claude Web

1. Додайте MCP server: `https://<mcp-ngrok-domain>/mcp`
2. Тип зʼєднання: **Streamable HTTP**
3. Натисніть Connect:
   - клієнт зареєструється через `/oauth/register/`
   - відкриється login/consent через `/oauth/authorize/`
   - токен буде отримано через `/oauth/token/`

Після цього tools працюють від імені **конкретного користувача**, який пройшов login/consent.

## 5) Troubleshooting

- `Failed to fetch`:
  - перевірте що ngrok для `9000` живий
  - `MCP_SERVER_PUBLIC_URL` збігається з публічним MCP доменом

- `401` на MCP:
  - перевірте `MCP_INTROSPECTION_SECRET == OAUTH_INTROSPECTION_SECRET`
  - перезапустіть `web` і `mcp` через `--force-recreate`
  - якщо в логах є `POST /... ?token=... 401`, це legacy-підключення. Query-token auth вимкнено; потрібно перепідключити конектор через OAuth login/consent.

- У списку tools `0 of 0`:
  - reconnect у клієнті
  - перевірте `https://<mcp-domain>/mcp` і `https://<web-domain>/.well-known/oauth-authorization-server`

## Legacy mode

`MCP_API_TOKEN` залишився як fallback для сумісності, але рекомендовано OAuth.
