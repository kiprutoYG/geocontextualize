# Deployment Guide: GeoContextualize

This guide explains how to deploy the GeoContextualize full-stack application to a remote server using Docker.

## 1. Prerequisites

Ensure your remote server has:
- **Ubuntu 22.04 LTS** (recommended)
- **Git**
- **Docker** & **Docker Compose**

### Install Docker & Docker Compose
If not already installed, run:
```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
# Log out and log back in for group changes to take effect
```

## 2. Server Setup

### Clone the Repository
```bash
git clone https://github.com/eugene-tulu/DescribeyourArea.git geocontextualize
cd geocontextualize
```

### Configure Environment Variables
Create a `.env` file in the root directory:
```bash
nano .env
```
Add your API keys:
```env
GEMINI_API_KEY=your_google_gemini_api_key_here
# This value is embedded into the Next.js frontend at image-build time.
# Use the public HTTPS API URL once a reverse proxy is configured.
NEXT_PUBLIC_BACKEND_URL=http://<your-server-ip>:8001
```

## 3. Deploying with Docker Compose

Run the application in detached mode:
```bash
docker compose up -d --build
```

### Verify Status
Check if containers are running:
```bash
docker compose ps
```
Check logs for the backend:
```bash
docker compose logs -f backend
```

## 4. Accessing the Application

- **Frontend:** `http://<your-server-ip>:3000`
- **Backend API:** `http://<your-server-ip>:8001`
- **Health Check:** `http://<your-server-ip>:8001/health`

If `NEXT_PUBLIC_BACKEND_URL` changes, rebuild the frontend image so the new value is included in the browser bundle:
```bash
docker compose up -d --build frontend
```

## 5. Production Considerations

### Reverse Proxy (Nginx)
For production, use **Nginx** or Caddy as a reverse proxy to handle HTTPS and route traffic to the frontend and backend. Set `NEXT_PUBLIC_BACKEND_URL` to the public HTTPS backend URL before rebuilding the frontend, then do not expose ports 3000 or 8001 publicly.

### Security
- **Firewall:** Open only ports 80, 443, and 22 (SSH). Close 3000 and 8001 to the public after the reverse proxy is in place.
- **Env Vars:** Never commit your `.env` file to Git.

### Data Persistence
The backend uses a `.cache` volume to store scene metadata and avoid redundant STAC searches. This is persisted via the Docker volume defined in `docker-compose.yml`.

## 6. Maintenance

### Updating the App
```bash
git pull
docker compose up -d --build
```

### Stopping the App
```bash
docker compose down
```
