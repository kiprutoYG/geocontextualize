# Deployment Guide: GeoContextualize

This guide explains how to deploy the GeoContextualize full-stack application to a remote server using Docker.

## 1. Prerequisites

Ensure your remote server has:

- **Ubuntu 22.04 LTS** (recommended)
- **Git**
- **Docker** and **Docker Compose**

### Install Docker and Docker Compose

If they are not already installed, run:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
```

Log out and back in for the group change to take effect.

## 2. Server setup

### Clone the repository

```bash
git clone https://github.com/kiprutoYG/geocontextualize.git
cd geocontextualize
```

### Configure environment variables

Create a `.env` file in the project root:

```bash
nano .env
```

Add the application key and public backend URL:

```env
GEMINI_API_KEY=your_google_gemini_api_key_here
# This value is embedded into the Next.js frontend at image-build time.
# Use the public HTTPS API URL once a reverse proxy is configured.
NEXT_PUBLIC_BACKEND_URL=http://<your-server-ip>:8001
```

## 3. Deploy with Docker Compose

Run the application in detached mode:

```bash
docker compose up -d --build
```

### Verify status

```bash
docker compose ps
docker compose logs -f backend
```

## 4. Access the application

- **Frontend:** `http://<your-server-ip>:3000`
- **Backend API:** `http://<your-server-ip>:8001`
- **Health check:** `http://<your-server-ip>:8001/health`

If `NEXT_PUBLIC_BACKEND_URL` changes, rebuild the frontend image so the browser bundle receives the new value:

```bash
docker compose up -d --build frontend
```

## 5. Production considerations

### Reverse proxy

For production, use **Nginx** or Caddy as a reverse proxy to handle HTTPS and route traffic to the frontend and backend. Set `NEXT_PUBLIC_BACKEND_URL` to the public HTTPS backend URL before rebuilding the frontend, then do not expose ports 3000 or 8001 publicly.

### Security

- **Firewall:** Open only ports 80, 443, and 22 (SSH). Close 3000 and 8001 to the public after the reverse proxy is in place.
- **Environment variables:** Never commit the `.env` file.

### Data persistence

The backend uses a `.cache` volume to store scene metadata and avoid redundant STAC searches. Docker Compose persists this volume.

## 6. Maintenance

### Update the application

```bash
git pull
docker compose up -d --build
```

### Stop the application

```bash
docker compose down
```
