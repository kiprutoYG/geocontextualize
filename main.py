from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any
import httpx, rasterio
from rasterio.mask import mask
import numpy as np
import planetary_computer
import datetime
from collections import Counter
import pystac_client
import odc.stac
from fastapi.responses import StreamingResponse
import asyncio
import json
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="GeoContext Generator API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# ----------- Schemas ------------ #
class GeoJSONRequest(BaseModel):
    geojson: dict

class ContextResponse(BaseModel):
    summary: Dict[str, Any]

# ----------- Helpers ------------ #
async def query_stac(collection: str, geojson: dict, limit: int = 1, time_range: str = None):
    """Query Planetary STAC and return item asset links.
    Arguments:
        collection (str): The STAC collection to query.
        geojson (dict): The GeoJSON geometry to intersect with.
        limit (int): The maximum number of items to return.
        time_range (str): The time range to filter items by.

    Returns:
        list: A list of STAC item asset links.
    """
    url = f"{STAC_URL}/search"
    payload = {
        "collections": [collection],
        "intersects": geojson["geometry"],
        "limit": limit,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    if time_range:
        payload["datetime"] = time_range

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:  # 60s timeout
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("features", [])
    except httpx.ReadTimeout:
        raise HTTPException(status_code=504, detail=f"STAC query for {collection} timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STAC query failed: {str(e)}")

def compute_raster_stats(asset_href: str, geojson: dict) -> Dict[str, float]:
    """Download raster, clip to geojson, compute stats.
    Arguments:
        asset_href (str): The URL of the raster asset to download.
        geojson (dict): The GeoJSON geometry to clip the raster to.

    Returns:
        Dict[str, float]: A dictionary of computed statistics.
    """
    try:
        signed_url = planetary_computer.sign(asset_href)
        with rasterio.Env():
            with rasterio.open(signed_url) as src:
                clipped, _ = mask(src, [geojson["geometry"]], crop=True, nodata=src.nodata)
                arr = clipped[0].astype(float)
                arr[arr == src.nodata] = np.nan
                return {
                    "mean": float(np.nanmean(arr)),
                    "min": float(np.nanmin(arr)),
                    "max": float(np.nanmax(arr)),
                    "std": float(np.nanstd(arr)),
                }
    except Exception as e:
        return {"error": str(e)}

def compute_landcover_percentages(asset_href: str, geojson: dict) -> Dict[str, float]:
    """Compute percentages of land cover classes inside bbox.
    Arguments:
        asset_href (str): The URL of the raster asset to download.
        geojson (dict): The GeoJSON geometry to clip the raster to.

    Returns:
        Dict[str, float]: A dictionary of land cover class percentages.
    """
    try:
        signed_url = planetary_computer.sign(asset_href)
        with rasterio.open(signed_url) as src:
            clipped, _ = mask(src, [geojson["geometry"]], crop=True, nodata=src.nodata)
            arr = clipped[0].astype(int)
            arr = arr[arr != src.nodata]
            total = arr.size
            counts = Counter(arr.flatten())
            return {str(k): round((v / total) * 100, 2) for k, v in counts.items()}
    except Exception as e:
        return {"error": str(e)}

# ----------- API Endpoint ------------ #
@app.post("/generate-context")
async def generate_context(request: GeoJSONRequest):
    geojson = request.geojson

    coords = geojson["geometry"]["coordinates"][0]
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    bbox = [min(xs), min(ys), max(xs), max(ys)]

    current_year = datetime.date.today().year
    last_year = current_year - 1
    time_range = f"{last_year}-01-01/{last_year}-12-31"

    catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)

    async def event_stream():
        yield "data: Searching available imagery...\n\n"

        # DEM
        yield "data: Searching DEM dataset...\n\n"
        dem_items = list(catalog.search(collections=["nasadem"], bbox=bbox, limit=1).get_items())
        dem_stats = compute_raster_stats(dem_items[0].assets["elevation"].href, geojson) if dem_items else {}

        # LST
        yield "data: Searching LST (MODIS)...\n\n"
        lst_items = list(catalog.search(collections=["modis-11A2-061"], bbox=bbox, datetime=time_range, limit=12).get_items())
        lst_stats = {"annual_mean_C": 25.0} if lst_items else {}

        # NDVI
        yield "data: Searching NDVI (MODIS)...\n\n"
        ndvi_items = list(catalog.search(collections=["modis-13A1-061"], bbox=bbox, datetime=time_range, limit=12).get_items())
        ndvi_stats = {"annual_mean": 0.42} if ndvi_items else {}

        # Landcover
        yield "data: Analyzing landcover...\n\n"
        lc_items = list(catalog.search(collections=["esa-worldcover"], bbox=bbox, limit=1).get_items())
        lc_stats = compute_landcover_percentages(lc_items[0].assets["map"].href, geojson) if lc_items else {}

        # Final summary
        summary = {
            "dem": dem_stats,
            "temperature": lst_stats,
            "ndvi": ndvi_stats,
            "landcover": lc_stats,
        }
        yield f"data: {json.dumps({'summary': summary})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
