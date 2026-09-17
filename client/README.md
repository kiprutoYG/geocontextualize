# GeoContext Frontend

This is the frontend for the GeoContext Generator API, built with Next.js.

## Configuration

The production frontend uses a same-origin API path:

```bash
NEXT_PUBLIC_BACKEND_URL=/api
```

Nginx routes `/api/` to the private backend service. This avoids embedding a
public server address in the browser bundle.

### Development

For local development with a locally running backend:

```bash
# Install dependencies
npm install

# Run the development server
npm run dev
```

The frontend calls `/api` by default. Next.js rewrites that path to
`http://127.0.0.1:8000` during local development. Docker builds pass
`BACKEND_PROXY_TARGET=http://backend:8000` so the same path works in Compose.

### Production

Build the standalone frontend image behind the reverse proxy:

```bash
docker compose up -d --build frontend
```

## Features

- Interactive map for selecting study areas
- GeoJSON upload capability
- Elevation analysis
- NDVI (Normalized Difference Vegetation Index) analysis
- Landcover classification
- AI-powered narrative descriptions
- Responsive design for all devices

## Deployment

For the server deployment, keep port 3000 loopback-only and serve it through
the HTTPS reverse proxy described in the repository deployment guide.
