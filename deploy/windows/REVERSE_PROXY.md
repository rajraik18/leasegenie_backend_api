# Reverse Proxy + TLS for LeaseGenie

The API binds to `127.0.0.1:8000` by default — uvicorn only accepts traffic from the same host. To expose the API on the network you put a reverse proxy in front that:

1. Terminates TLS (presents the certificate to clients).
2. Forwards plain HTTP to `127.0.0.1:8000`.
3. Sets `X-Forwarded-Proto`, `X-Forwarded-For`, `X-Forwarded-Host` so the API knows the original scheme / client.
4. Optionally adds a per-request `X-Request-ID` (the API generates one if missing — see `app/main.py::RequestIdMiddleware`).

Three battle-tested options on Windows are below. **Pick one.**

---

## Option 1 — Caddy (simplest, automatic TLS via Let's Encrypt)

Install Caddy: <https://caddyserver.com/download>. Single `caddy.exe`. Save as a Windows Service via NSSM if you want auto-start.

`Caddyfile`:

```caddy
api.example.com {
    encode gzip

    # Forward everything to uvicorn
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Real-IP {remote}
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-For   {remote}
    }

    # Lock down TLS to modern ciphers only
    tls {
        protocols tls1.2 tls1.3
    }

    # Hide internal endpoints from the public internet
    @internal {
        path /readiness /metrics
    }
    handle @internal {
        # Allow only RFC1918 + loopback
        @allowed remote_ip 127.0.0.1/32 10.0.0.0/8 192.168.0.0/16 172.16.0.0/12
        handle @allowed {
            reverse_proxy 127.0.0.1:8000
        }
        respond 403
    }
}
```

Run: `caddy run --config Caddyfile`. Caddy will request and renew the certificate from Let's Encrypt automatically (port 80 + 443 must be reachable).

---

## Option 2 — IIS (corporate / AD environments)

1. Install **URL Rewrite** + **Application Request Routing (ARR)** modules from <https://www.iis.net/downloads>.
2. Enable ARR proxy: IIS Manager → server node → Application Request Routing Cache → Server Proxy Settings → check **Enable proxy** → set **Reverse rewrite host in response headers**.
3. Create a site bound to `https://api.example.com:443` with your ACM / corporate cert.
4. In the site's `web.config`:

```xml
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="ProxyToLeaseGenie" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:8000/{R:1}" />
          <serverVariables>
            <set name="HTTP_X_FORWARDED_PROTO" value="https" />
            <set name="HTTP_X_FORWARDED_HOST"  value="{HTTP_HOST}" />
            <set name="HTTP_X_FORWARDED_FOR"   value="{REMOTE_ADDR}" />
          </serverVariables>
        </rule>
      </rules>
    </rewrite>
    <security>
      <requestFiltering>
        <!-- Match settings.max_upload_total_mb in .env (500 MB by default) -->
        <requestLimits maxAllowedContentLength="524288000" />
      </requestFiltering>
    </security>
  </system.webServer>
</configuration>
```

5. **Allow the proxy headers in IIS**: IIS Manager → site → Configuration Editor → `system.webServer/rewrite/allowedServerVariables` → add `HTTP_X_FORWARDED_PROTO`, `HTTP_X_FORWARDED_HOST`, `HTTP_X_FORWARDED_FOR`.

---

## Option 3 — nginx for Windows (lightweight)

Download <http://nginx.org/en/download.html>. `conf/nginx.conf` (relevant chunk):

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate     C:/path/to/fullchain.pem;
    ssl_certificate_key C:/path/to/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 500M;            # match MAX_UPLOAD_TOTAL_MB

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_set_header   X-Forwarded-Host  $host;

        # Long-running uploads / extractions
        proxy_read_timeout    600s;
        proxy_send_timeout    600s;
        proxy_request_buffering off;
    }

    # Block /readiness and /metrics from the public internet
    location ~ ^/(readiness|metrics)$ {
        allow 127.0.0.1;
        allow 10.0.0.0/8;
        allow 192.168.0.0/16;
        deny  all;
        proxy_pass http://127.0.0.1:8000;
    }
}

server {
    listen 80;
    server_name api.example.com;
    return 301 https://$host$request_uri;
}
```

Register `nginx.exe` as a service via NSSM.

---

## What the API expects from the proxy

- **Listen address**: `127.0.0.1:8000` is the default (set `API_HOST=0.0.0.0` only if you have a hardware firewall). The API does not negotiate TLS itself.
- **Request size**: the API enforces per-file (`MAX_UPLOAD_SIZE_MB=100`) and per-request (`MAX_UPLOAD_TOTAL_MB=500`) caps in `app/api/v1/extract_pdf.py`. Make sure the proxy's `client_max_body_size` / `maxAllowedContentLength` is at least as high.
- **Timeout**: extraction jobs are async (Celery handles them), so the API itself returns within seconds, but the upload can be slow on big PDFs. Use ≥ 600 s.
- **Auth**: the API enforces `API_KEY` from `.env` if set. Clients must present it via either `Authorization: Bearer <key>` or `X-API-Key: <key>`. The reverse proxy can also inject it for trusted internal callers.
- **CORS**: configured in `.env::CORS_ALLOW_ORIGINS` — list the exact public origins (e.g. `https://app.example.com`). Don't use `*`.

---

## Deny-list for the public surface

| Path | Should be public? | How to gate |
|---|---|---|
| `/health` | Yes (load-balancer probe) | — |
| `/readiness` | No (internal probe) | Restrict by IP in proxy (examples above) |
| `/metrics` | No (Prometheus scrape) | Restrict by IP in proxy |
| `/docs`, `/redoc`, `/openapi.json` | **No** in production | Already gated: only served when `DEBUG=true` in `.env` |
| `/api/v1/*` | Yes | Auth via `X-API-Key` / `Authorization` header |

---

## Verifying the proxy

```powershell
# From the host (skips proxy):
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing

# From outside (through proxy):
Invoke-WebRequest https://api.example.com/health -UseBasicParsing

# Confirm /docs is gated:
(Invoke-WebRequest https://api.example.com/docs -SkipHttpErrorCheck).StatusCode    # expect 404

# Confirm auth is enforced (when API_KEY is set):
(Invoke-WebRequest https://api.example.com/api/v1/orders/00000000 -SkipHttpErrorCheck).StatusCode    # expect 401
(Invoke-RestMethod https://api.example.com/api/v1/orders/00000000 -Headers @{ "X-API-Key" = "<your-key>" }) # expect 404 (not 401)
```
