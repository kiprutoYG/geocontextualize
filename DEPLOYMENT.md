# Deployment guide

The production stack is intentionally small: a FastAPI backend and a Next.js
frontend run on loopback-only Docker ports, while Nginx is the only public
entry point. Browser calls use the same-origin `/api` path.

## Prerequisites

- Ubuntu with Docker Compose v2, Git, and Nginx
- A real `.env` file containing `GEMINI_API_KEY`; copy `.env.example`, do not
  commit the resulting file
- A public HTTPS hostname is preferred. The checked-in Nginx examples also
  support the current server IP as a short-lived certificate fallback.

## Deploy the `main` source

```bash
git clone --branch main https://github.com/eugene-tulu/DescribeyourArea.git /srv/geocontextualize
cd /srv/geocontextualize
cp .env.example .env
# Set GEMINI_API_KEY and, if needed, the explicit CORS_ORIGINS.
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8001/health
```

The Compose file deliberately binds only `127.0.0.1:3000` and
`127.0.0.1:8001`. It also caps application resources, retains only small local
logs, and has no disk-growing STAC cache.

## Nginx

Install `deploy/nginx/geocontextualize-rate-limit.conf` under
`/etc/nginx/conf.d/`. Use `geocontextualize-http.conf` while issuing a
certificate, then replace it with `geocontextualize-ip.conf` (or an equivalent
named-host configuration). Test every change before reloading:

```bash
nginx -t && systemctl reload nginx
```

The proxy strips the `/api/` prefix before forwarding to FastAPI, applies
per-IP request and connection limits, and keeps Docker service ports private.

## Verify and maintain

```bash
curl --fail https://209.38.197.161/api/health
docker compose logs --tail=100 backend
docker compose up -d --build
```

The API admits only polygonal GeoJSON. Defaults are a 500 KB payload, 10,000
vertices, a 100 km² synchronous bounding-box cap, and a 10 km² NDVI cap. The
application returns an explicit skipped NDVI result above that smaller cap;
large asynchronous analyses require a separately deployed durable queue and
worker.

Open only SSH, HTTP, and HTTPS in the firewall after confirming the Nginx
route. Do not expose ports 3000 or 8001 publicly.
