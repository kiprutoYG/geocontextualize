# GeoContextualize

A geospatial analysis API that generates contextual information about any location on Earth using satellite imagery and geospatial datasets.

## Features

- Elevation analysis using NASADEM
- Landcover classification using ESA WorldCover
- Bounded NDVI (Normalized Difference Vegetation Index) analysis using Sentinel-2 data
- AI-powered narrative descriptions using Google Gemini
- Support for Polygon, MultiPolygon, and polygon FeatureCollection study areas
- Docker deployment behind a same-origin HTTPS reverse proxy

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md). Production Docker ports are loopback-only;
Nginx serves the frontend and routes `/api/` to the backend.

## Backend (API)

Built with FastAPI, the backend provides:

- `/generate-context` - Main endpoint for generating geospatial context
- `/health` - Health check endpoint
- `/version` - Version information endpoint
- CORS support for web applications
- Request-size, vertex, bounding-box, concurrency, memory, and timeout limits

## Frontend

The frontend is a Next.js application located in the `client/` directory that provides:

- Interactive map interface
- GeoJSON upload capability
- Visual feedback for analysis results
- Responsive design

## Technologies Used

### Backend
- FastAPI
- Rasterio
- PySTAC Client
- Microsoft Planetary Computer
- ODC STAC
- Google Generative AI
- XArray, RioxArray

### Frontend
- Next.js
- React
- Leaflet
- Tailwind CSS

## Setup

### Backend Setup

1. Clone the repository
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up environment variables:
   ```bash
   cp .env.example .env
   # Edit .env to add your GEMINI_API_KEY
   ```
4. Run the server:
   ```bash
   uvicorn main:app --reload
   ```

### Frontend Setup

1. Navigate to the client directory:
   ```bash
   cd client
   ```
2. Install dependencies:
   ```bash
   npm install
   ```
3. The local default routes through `/api`; Next.js proxies that route to
   `http://127.0.0.1:8000` during development. To use another backend, set:
   ```bash
   export NEXT_PUBLIC_BACKEND_URL=https://your-api.example.org
   ```
4. Run the development server:
   ```bash
   npm run dev
   ```

## API Endpoints

- `POST /generate-context` - Generate geospatial context for a GeoJSON area
- `GET /health` - Health check
- `GET /version` - Version information

## Parameters

The `/generate-context` endpoint accepts:
- `geojson`: GeoJSON object defining the area of interest
- `include_narrative`: Boolean to include AI-generated narrative
- `audience`: Target audience for narrative ("academic", "investor", "farmer", "policy")
- `include_ndvi`: Boolean to include NDVI analysis

## Architecture

The system leverages Microsoft Planetary Computer to access:
- NASADEM for elevation data
- ESA WorldCover for landcover classification
- Sentinel-2 L2A for NDVI analysis
- no whole-raster fallback; unavailable or oversized NDVI requests return an explicit status

## Constraints

The live synchronous service is deliberately bounded:

- Polygon and MultiPolygon inputs are validated and FeatureCollections are unioned.
- A request is rejected when its bounding box exceeds 100 km² by default.
- NDVI is skipped (with an explicit warning) when its bounding box exceeds 10 km².
- Sentinel-2 searches retrieve at most four cloud-filtered scenes and retry only
  short transient catalogue errors.
- One analysis is admitted at a time by default to protect the server and shared
  Planetary Computer resources.

Large-area or batch analyses need a durable asynchronous job system before they
should be accepted.
