# Deployment guide

GeoContextualize runs as two private Docker services behind an HTTPS reverse
proxy. The browser always calls the same-origin `/api` route; Docker does not
publish the application ports to the internet.

## 1. Prepare the server

Install Docker Engine with the Compose plugin, Git, and Nginx. Clone the
deployment branch explicitly:

```bash
git clone --branch tulu https://github.com/kiprutoYG/geocontextualize.git /srv/geocontextualize
cd /srv/geocontextualize
cp .env.example .env
```

Edit `.env` and set `GEMINI_API_KEY` if narrative generation is needed.
Keep the remaining limits conservative unless a job queue has been added:

```env
GEMINI_API_KEY=
CORS_ORIGINS=https://geo.example.org
NEXT_PUBLIC_BACKEND_URL=/api
MAX_SYNC_BBOX_KM2=100
MAX_NDVI_BBOX_KM2=10
MAX_CONCURRENT_ANALYSES=1
MAX_PC_SCENES=4
```

Do not commit `.env`.

## 2. Run the private services

```bash
docker compose config
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8001/health
```

The backend binds only to `127.0.0.1:8001`, and the frontend only to
`127.0.0.1:3000`. This is intentional: Nginx is the sole public entry point.
The Compose configuration also limits container memory/CPU/processes and
rotates application logs.

## 3. Configure Nginx and TLS

Use a real hostname with direct DNS to the server and obtain a TLS certificate
before public launch. The tracked `deploy/nginx/` directory includes a
rate-limit file plus direct-IP bootstrap/final examples for the current server;
use a named-host variant for a durable deployment. Add a server block such as:

```nginx
server {
    listen 443 ssl http2;
    server_name geo.example.org;

    # Configure the certificate paths managed by your ACME client here.
    ssl_certificate     /etc/letsencrypt/live/geo.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/geo.example.org/privkey.pem;

    client_max_body_size 600k;

    location = /api {
        return 308 /api/;
    }

    location ^~ /api/ {
        limit_req zone=geo_api burst=10 nodelay;
        proxy_pass http://127.0.0.1:8001/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Define the request-rate zone once inside Nginx's `http {}` context (for
example in `/etc/nginx/conf.d/geocontextualize-rate.conf`):

```nginx
limit_req_zone $binary_remote_addr zone=geo_api:10m rate=10r/m;
```

Test and reload only after the certificate and server configuration are in
place:

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -fsS https://geo.example.org/api/health
```

Allow only SSH, HTTP, and HTTPS through the host firewall. Do not expose
ports 3000 or 8001.

## 4. Updates and rollback

```bash
cd /srv/geocontextualize
git pull --ff-only origin tulu
docker compose up -d --build
docker compose ps
```

Check `/api/health` before removing prior images. A request exceeding the
configured synchronous bounding-box limit is rejected; NDVI is reported as
skipped above its tighter limit. Those areas need to be split or processed by
a future asynchronous job system rather than retried repeatedly.
