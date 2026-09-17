# GeoContextualize

GeoContextualize turns a polygonal study area into a concise geospatial
context: terrain from NASADEM, land cover from ESA WorldCover, and a bounded
Sentinel-2 NDVI composite from Microsoft Planetary Computer. Optional modules
add soil, population, climate, hydrology, country context, and a Gemini
narrative.

## Reliable-by-default analysis

- Accepts GeoJSON `Polygon`, `MultiPolygon`, `Feature`, and `FeatureCollection`.
  FeatureCollection polygons are combined rather than silently discarding all
  but the first feature.
- Rejects malformed, oversized, or overly complex inputs before calling an
  external service.
- Caps synchronous bounding boxes at 100 km² by default and NDVI at 10 km².
- Searches at most four recent cloud-filtered Sentinel-2 scenes and clips data
  to the submitted geometry before calculating the median.
- Uses Planetary Computer only for NDVI. There is no EOPF or unsafe full-raster
  MODIS fallback.
- Limits concurrent analyses so one request cannot exhaust the server or shared
  public data services.

Large study areas are not silently downgraded: NDVI returns a clear `skipped`
status. Supporting large asynchronous analysis needs a durable job queue and
worker, which is deliberately outside this synchronous service.

## Local setup

```bash
cp .env.example .env
# Add GEMINI_API_KEY if narrative generation is needed.
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

In a second terminal:

```bash
cd client
npm ci
npm run dev
```

The frontend uses `/api` by default. Next.js rewrites that path to the local
backend in development. Set `NEXT_PUBLIC_BACKEND_URL` only when intentionally
using a different API origin.

## API

`POST /generate-context` accepts a JSON body with `geojson` and optional query
parameters:

- `datasets=dem,landcover,ndvi` selects which data modules run. Available
  values are `dem`, `landcover`, `ndvi`, `soils`, `population`, `climate`, and
  `hydrology`; omitted defaults to the three core modules.
- `include_ndvi=false` skips NDVI even if it is selected.
- `include_narrative=true` enables Gemini narrative generation.
- `audience=academic|investor|farmer|policy` selects the narrative audience.

`GET /health` reports service readiness and `GET /version` describes active
limits.

## Production

See [DEPLOYMENT.md](DEPLOYMENT.md). The Compose configuration binds app ports
only to loopback and expects Nginx to proxy the frontend and `/api/` route.
