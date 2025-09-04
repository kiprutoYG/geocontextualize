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

app = FastAPI(title="GeoContext Generator API")

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
@app.post("/generate-context", response_model=ContextResponse)
async def generate_context(request: GeoJSONRequest):
    geojson = request.geojson
    bbox = [
        min([c[0] for c in geojson["geometry"]["coordinates"][0]]),
        min([c[1] for c in geojson["geometry"]["coordinates"][0]]),
        max([c[0] for c in geojson["geometry"]["coordinates"][0]]),
        max([c[1] for c in geojson["geometry"]["coordinates"][0]]),
    ]

    # determine last full year
    current_year = datetime.date.today().year
    last_year = current_year - 1
    time_range = f"{last_year}-01-01/{last_year}-12-31"

    catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace,)

    # DEM
    dem_search = catalog.search(collections=["nasadem"], bbox=bbox, limit=1)
    dem_items = list(dem_search.get_items())
    dem_stats = compute_raster_stats(dem_items[0].assets["elevation"].href, geojson) if dem_items else {}

    # # Rainfall (TerraClimate) - sample up to 12 scenes
    # rain_search = catalog.search(collections=["terraclimate"], bbox=bbox, datetime=time_range, limit=12)
    # rain_items = list(rain_search.get_items())
    # rainfall_stats = {}
    # if rain_items:
    #     vals = []
    #     for feat in rain_items:
    #         if "precipitation" in feat.assets:
    #             st = compute_raster_stats(feat.assets["precipitation"].href, geojson)
    #             if "mean" in st:
    #                 vals.append(st["mean"])
    #     if vals:
    #         rainfall_stats = {
    #             "annual_total": float(np.nansum(vals)),
    #             "annual_mean": float(np.nanmean(vals)),
    #         }

    # # LST (MODIS 21A2.061) - sample up to 12 images for the year
    lst_search = catalog.search(collections=["modis-11A2-061"], bbox=bbox, datetime=time_range, limit=12)
    lst_items = list(lst_search.get_items())
    lst_stats = {}
    if lst_items:
        signed = [planetary_computer.sign(item) for item in lst_items]
        ds = odc.stac.load(
            signed,
            bands=["LST_Day_1km"],
            crs="EPSG:3857",
            resolution=1000,
            bbox=bbox
        )
        if ds["LST_Day_1km"].size > 0:
            scale = lst_items[0].assets["LST_Day_1km"].extra_fields["raster:bands"][0]["scale"]
            arr = (ds["LST_Day_1km"].values * scale) - 273.15  # Kelvin → °C
            raw_arr = ds['LST_Day_1km'].values
            arr = np.where(raw_arr == 0, np.nan, arr)
            lst_stats = {
                "annual_mean_C": float(np.nanmean(arr)),
                "min_C": float(np.nanmin(arr)),
                "max_C": float(np.nanmax(arr)),
            }

    # NDVI (MODIS 13A1.061) - sample up to 24 images for the year
    ndvi_search = catalog.search(collections=["modis-13A1-061"], bbox=bbox, datetime=time_range, limit=12)
    ndvi_items = list(ndvi_search.get_items())
    ndvi_stats = {}
    if ndvi_items:
        signed = [planetary_computer.sign(item) for item in ndvi_items]
        ds = odc.stac.load(
            signed,
            bands=["500m_16_days_NDVI"],
            crs="EPSG:3857",
            resolution=500,
            bbox=bbox
        )
        if ds["500m_16_days_NDVI"].size > 0:
            scale = ndvi_items[0].assets["500m_16_days_NDVI"].extra_fields["raster:bands"][0]["scale"]
            arr = ds["500m_16_days_NDVI"].values * scale
            ndvi_stats = {
                "annual_mean": float(np.nanmean(arr))
            }

    # Landcover
    lc_search = catalog.search(collections=["esa-worldcover"], bbox=bbox, limit=1)
    lc_items = list(lc_search.get_items())
    lc_stats = compute_landcover_percentages(lc_items[0].assets["map"].href, geojson) if lc_items else {}


    return ContextResponse(summary={
        "dem": dem_stats,
        # "rainfall": rainfall_stats,
        "temperature": lst_stats,
        "ndvi": ndvi_stats,
        "landcover": lc_stats
    })
    
@app.post("/analyze")
async def analyze(context: ContextResponse):
    # here context.summary is your stats dictionary
    global last_analysis
    last_analysis = {
        "summary": context.summary
    }
    return last_analysis


@app.get("/analysis")
async def get_analysis():
    if last_analysis["summary"] is None:
        return {"message": "No analysis available yet. Please POST to /analyze first."}
    return last_analysis
