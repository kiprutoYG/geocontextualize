# --------------------------------------------------
# IMPORTS (CRITICAL FIX: rioxarray import)
# --------------------------------------------------
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional, Literal
import rasterio as rio
from rasterio.mask import mask
import numpy as np
import planetary_computer
import datetime
from collections import Counter
import pystac_client
import asyncio
import json
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv
import google.generativeai as genai
import sys
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from pyproj import Geod
import requests

# Datacube imports (CRITICAL: rioxarray registers .rio accessor)
from odc.stac import load as stac_load
import rioxarray  # MUST be imported to enable .rio methods

# Optional imports for external datasets
try:
    import overpy
    OVERPY_AVAILABLE = True
except ImportError:
    OVERPY_AVAILABLE = False

# --------------------------------------------------
# ENVIRONMENT
# --------------------------------------------------
load_dotenv()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer setting without making a bad env value fatal."""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read a bounded float setting without making a bad env value fatal."""
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


# These caps keep synchronous browser requests predictable. A durable worker and
# queue are required before offering larger, asynchronous study areas.
MAX_GEOJSON_BYTES = _env_int("MAX_GEOJSON_BYTES", 500_000)
MAX_AOI_VERTICES = _env_int("MAX_AOI_VERTICES", 10_000)
MAX_SYNC_BBOX_KM2 = _env_float("MAX_SYNC_BBOX_KM2", 100.0)
MAX_NDVI_BBOX_KM2 = _env_float("MAX_NDVI_BBOX_KM2", 10.0)
MAX_PC_SCENES = _env_int("MAX_PC_SCENES", 4)
MAX_CONCURRENT_ANALYSES = _env_int("MAX_CONCURRENT_ANALYSES", 1)
ANALYSIS_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)
WGS84_GEOD = Geod(ellps="WGS84")

# --------------------------------------------------
# APP CONFIGURATION
# --------------------------------------------------
app = FastAPI(title="GeoContext Generator API")

# Explicit origins are required because the API permits credentialed requests.
# Production uses same-origin /api behind Nginx, but this also supports local dev.
origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CRITICAL FIX: Strip whitespace from STAC URL
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1    ".strip()


@app.middleware("http")
async def limit_analysis_concurrency(request: Request, call_next):
    """Bound costly STAC/raster work before it reaches shared public services."""
    if request.url.path != "/generate-context":
        return await call_next(request)

    try:
        await asyncio.wait_for(ANALYSIS_SEMAPHORE.acquire(), timeout=2.0)
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=429,
            content={"detail": "Analysis capacity is busy. Please retry shortly."},
        )

    try:
        return await call_next(request)
    finally:
        ANALYSIS_SEMAPHORE.release()

# --------------------------------------------------
# SCHEMAS
# --------------------------------------------------
class GeoJSONRequest(BaseModel):
    geojson: dict

class ContextResponse(BaseModel):
    summary: Dict[str, Any]
    narrative: Optional[str] = None

# --------------------------------------------------
# LANDCOVER LOOKUP
# --------------------------------------------------
ESA_WORLDCOVER_CLASSES = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up areas",
    60: "Bare or sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetlands",
    95: "Mangroves",
    100: "Moss and lichen",
}

def label_landcover(percentages: Dict[str, float]) -> Dict[str, float]:
    return {
        ESA_WORLDCOVER_CLASSES.get(int(code), f"Unknown ({code})"): pct
        for code, pct in percentages.items()
    }

# --------------------------------------------------
# AOI ADMISSION AND GEOMETRY HELPERS
# --------------------------------------------------
def _position_count(coordinates: Any) -> int:
    """Count GeoJSON positions without assuming Polygon nesting depth."""
    if not isinstance(coordinates, (list, tuple)):
        return 0
    if coordinates and all(isinstance(value, (int, float)) for value in coordinates):
        return 1
    return sum(_position_count(value) for value in coordinates)


def _polygon_geometry(value: Any):
    """Return a validated Shapely Polygon/MultiPolygon or raise a client error."""
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Each GeoJSON feature must be an object")

    geometry = value.get("geometry") if value.get("type") == "Feature" else value
    if not isinstance(geometry, dict):
        raise HTTPException(status_code=400, detail="GeoJSON feature is missing a geometry")
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(
            status_code=400,
            detail="Only Polygon and MultiPolygon study areas are supported",
        )

    try:
        geom = shape(geometry)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid GeoJSON geometry") from exc

    if geom.is_empty or not geom.is_valid:
        raise HTTPException(status_code=400, detail="Study-area geometry is empty or invalid")
    return geom


def canonicalize_geojson(
    geojson: dict,
    *,
    max_bytes: int = MAX_GEOJSON_BYTES,
    max_vertices: int = MAX_AOI_VERTICES,
) -> dict:
    """Canonicalize supported GeoJSON into one Feature for every raster call.

    FeatureCollections are unioned rather than silently discarding all but the
    first polygon. This also lets uploaded MultiPolygons follow the same path
    as shapes drawn in the browser.
    """
    if not isinstance(geojson, dict):
        raise HTTPException(status_code=400, detail="GeoJSON must be an object")

    try:
        payload_size = len(json.dumps(geojson, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="GeoJSON is not JSON serializable") from exc
    if payload_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Study-area payload exceeds the {max_bytes} byte limit",
        )

    input_type = geojson.get("type")
    if input_type == "FeatureCollection":
        features = geojson.get("features")
        if not isinstance(features, list) or not features:
            raise HTTPException(status_code=400, detail="FeatureCollection must contain polygons")
        geometries = [_polygon_geometry(feature) for feature in features]
        properties: dict[str, Any] = {}
    elif input_type in {"Feature", "Polygon", "MultiPolygon"}:
        geometries = [_polygon_geometry(geojson)]
        properties = geojson.get("properties", {}) if input_type == "Feature" else {}
        if not isinstance(properties, dict):
            properties = {}
    else:
        raise HTTPException(
            status_code=400,
            detail="GeoJSON must be a Feature, FeatureCollection, Polygon, or MultiPolygon",
        )

    vertex_count = sum(_position_count(mapping(geom).get("coordinates")) for geom in geometries)
    if vertex_count > max_vertices:
        raise HTTPException(
            status_code=413,
            detail=f"Study area has too many vertices (limit: {max_vertices})",
        )

    merged = unary_union(geometries)
    if merged.is_empty or merged.geom_type not in {"Polygon", "MultiPolygon"} or not merged.is_valid:
        raise HTTPException(status_code=400, detail="Study-area polygons cannot be combined safely")

    return {
        "type": "Feature",
        "properties": properties,
        "geometry": mapping(merged),
    }


def aoi_bbox(feature: dict) -> list[float]:
    """Derive bounds from canonical geometry; handles Polygon and MultiPolygon."""
    geom = _polygon_geometry(feature)
    minx, miny, maxx, maxy = geom.bounds
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90 or minx >= maxx or miny >= maxy:
        raise HTTPException(status_code=400, detail="Study-area coordinates must be valid WGS84 longitude/latitude")
    return [float(minx), float(miny), float(maxx), float(maxy)]


def _bbox_area_km2(bbox: list[float]) -> float:
    """Geodesic area of the bounding box used by STAC and raster reads."""
    minx, miny, maxx, maxy = bbox
    area_m2, _ = WGS84_GEOD.geometry_area_perimeter(box(minx, miny, maxx, maxy))
    return abs(area_m2) / 1_000_000


def validate_aoi(
    geojson: dict,
    *,
    max_bytes: int = MAX_GEOJSON_BYTES,
    max_vertices: int = MAX_AOI_VERTICES,
    max_bbox_km2: float = MAX_SYNC_BBOX_KM2,
) -> dict:
    """Apply payload, vertex, and synchronous bounding-box admission limits."""
    feature = canonicalize_geojson(
        geojson,
        max_bytes=max_bytes,
        max_vertices=max_vertices,
    )
    bbox = aoi_bbox(feature)
    bbox_area_km2 = _bbox_area_km2(bbox)
    if bbox_area_km2 > max_bbox_km2:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Study-area bounding box is {bbox_area_km2:.1f} km²; "
                f"the synchronous limit is {max_bbox_km2:g} km²"
            ),
        )
    return {
        "feature": feature,
        "bbox": bbox,
        "bbox_area_km2": bbox_area_km2,
    }


def normalize_geojson(geojson: dict) -> dict:
    """Backward-compatible name for callers that only need the canonical feature."""
    return canonicalize_geojson(geojson)

def compute_raster_stats(asset_href: str, geojson: dict) -> Dict[str, float]:
    try:
        signed_url = planetary_computer.sign(asset_href)
        with rio.open(signed_url) as src:
            clipped, _ = mask(
                src,
                [geojson["geometry"]],
                crop=True,
                nodata=src.nodata,
            )
            arr = clipped[0].astype(float)
            arr[arr == src.nodata] = np.nan

            return {
                "mean": float(np.nanmean(arr)),
                "min": float(np.nanmin(arr)),
                "max": float(np.nanmax(arr)),
                "std": float(np.nanstd(arr)),
            }
    except Exception as e:
        print(f"DEM computation error: {str(e)}", file=sys.stderr)
        return {"error": str(e)}

def interpret_terrain(dem: Dict[str, float]) -> Dict[str, Any]:
    if not dem or "mean" not in dem:
        return dem

    elevation_range = dem["max"] - dem["min"]

    if elevation_range < 50:
        terrain = "relatively flat"
    elif elevation_range < 300:
        terrain = "moderately undulating"
    else:
        terrain = "highly variable or mountainous"

    return {
        **dem,
        "elevation_range_m": round(elevation_range, 1),
        "terrain_type": terrain,
    }

def compute_landcover_percentages(asset_href: str, geojson: dict) -> Dict[str, Any]:
    try:
        signed_url = planetary_computer.sign(asset_href)
        with rio.open(signed_url) as src:
            clipped, _ = mask(
                src,
                [geojson["geometry"]],
                crop=True,
                nodata=src.nodata,
            )
            arr = clipped[0].astype(int)
            arr = arr[arr != src.nodata]

            if arr.size == 0:
                return {"error": "No valid landcover pixels"}

            counts = Counter(arr.flatten())
            total = arr.size

            percentages = {
                str(k): round((v / total) * 100, 2)
                for k, v in counts.items()
            }

            labeled = label_landcover(percentages)
            dominant_class = max(labeled, key=labeled.get)

            return {
                "classes": labeled,
                "dominant_class": dominant_class,
                "dominant_percentage": labeled[dominant_class],
            }
    except Exception as e:
        print(f"Landcover computation error: {str(e)}", file=sys.stderr)
        return {"error": str(e)}

# --------------------------------------------------
# NDVI COMPUTATION: BOUNDED PLANETARY COMPUTER WORKFLOW
# --------------------------------------------------
async def _search_sentinel_items(bbox: list[float], max_scenes: int) -> list:
    """Fetch a deliberately small scene set, retrying only transient failures."""
    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(days=90)
    time_window = f"{start.date().isoformat()}/{end.date().isoformat()}"

    def search_once() -> list:
        catalog = pystac_client.Client.open(STAC_URL)
        search = catalog.search(
            collections=["sentinel-2-l2a"],
            bbox=bbox,
            datetime=time_window,
            query={"eo:cloud_cover": {"lt": 30}},
            limit=max_scenes,
            max_items=max_scenes,
        )
        return list(search.items())

    for attempt in range(3):
        try:
            items = await asyncio.to_thread(search_once)
            return sorted(
                items[:max_scenes],
                key=lambda item: item.properties.get("eo:cloud_cover", 100),
            )
        except Exception as exc:
            if attempt == 2:
                print(
                    f"⚠️ Planetary Computer scene search failed: {type(exc).__name__}: {str(exc)[:120]}",
                    file=sys.stderr,
                )
                return []
            await asyncio.sleep(0.5 * (2 ** attempt))
    return []


def _load_and_summarize_ndvi(
    items: list,
    bbox: list[float],
    geojson_geom: dict,
    resolution_m: int,
) -> dict:
    """Load only the bounded AOI, mask clouds before reduction, and summarize."""
    resolution_deg = resolution_m / 111_320.0
    data = stac_load(
        items,
        bands=["B04", "B08", "SCL"],
        bbox=bbox,
        crs="EPSG:4326",
        resolution=resolution_deg,
        chunks={"x": 512, "y": 512},
        patch_url=planetary_computer.sign,
        dtype="uint16",
        groupby="solar_day",
        skip_broken=True,
    )
    if data is None or data.sizes.get("time", 0) == 0:
        return {
            "status": "unavailable",
            "source": "Planetary Computer",
            "warning": "No usable Sentinel-2 pixels were available for this study area.",
        }

    clipped = data.rio.write_crs("EPSG:4326").rio.clip(
        [geojson_geom],
        crs="EPSG:4326",
        all_touched=True,
        drop=True,
    )
    valid_scene_classes = clipped["SCL"].isin([4, 5, 6, 7, 11])
    # Sentinel reflectance arrives as uint16. Cast before subtracting so
    # negative differences cannot wrap to a large unsigned value.
    red = clipped["B04"].astype("float32")
    nir = clipped["B08"].astype("float32")
    ndvi = ((nir - red) / (nir + red + 1e-8)).where(
        valid_scene_classes
    )
    median_ndvi = ndvi.median(dim="time", skipna=True)
    values = median_ndvi.values.astype(float)
    values = values[np.isfinite(values) & (values > -1) & (values < 1)]
    if values.size == 0:
        return {
            "status": "unavailable",
            "source": "Planetary Computer",
            "warning": "Cloud and scene-quality masking left no valid Sentinel-2 pixels.",
        }

    scene_dates = [
        item.datetime.isoformat() if getattr(item, "datetime", None) else None
        for item in items
    ]
    return {
        "status": "ok",
        "source": "Planetary Computer",
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "std": float(np.std(values)),
        "p25": float(np.percentile(values, 25)),
        "p75": float(np.percentile(values, 75)),
        "scene_count": len(items),
        "resolution_m": resolution_m,
        "method": "sentinel_2_median_composite",
        "scene_ids": [item.id for item in items],
        "scene_dates": [scene_date for scene_date in scene_dates if scene_date],
    }


async def compute_median_ndvi(
    bbox: list[float],
    geojson_geom: dict,
    max_area_km2: float = MAX_NDVI_BBOX_KM2,
    max_scenes: int = MAX_PC_SCENES,
    resolution_m: int = 20,
) -> dict:
    """Compute a small Sentinel-2 composite from Planetary Computer only.

    A skipped result is explicit: the application never falls back to an
    unbounded MODIS raster read for a large study area.
    """
    bbox_area_km2 = _bbox_area_km2(bbox)
    if bbox_area_km2 > max_area_km2:
        return {
            "status": "skipped",
            "source": "Planetary Computer",
            "bbox_area_km2": round(bbox_area_km2, 2),
            "warning": (
                f"NDVI is available for bounding boxes up to {max_area_km2:g} km²; "
                "use a smaller study area for this synchronous analysis."
            ),
        }

    bounded_scenes = min(MAX_PC_SCENES, max(1, int(max_scenes)))
    items = await _search_sentinel_items(bbox, bounded_scenes)
    if not items:
        return {
            "status": "unavailable",
            "source": "Planetary Computer",
            "warning": "No cloud-filtered Sentinel-2 scenes were available in the last 90 days.",
        }

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_load_and_summarize_ndvi, items, bbox, geojson_geom, resolution_m),
            timeout=75.0,
        )
    except asyncio.TimeoutError:
        return {
            "status": "unavailable",
            "source": "Planetary Computer",
            "warning": "NDVI processing timed out; try a smaller study area shortly.",
        }
    except Exception as exc:
        print(f"⚠️ NDVI processing failed: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
        return {
            "status": "unavailable",
            "source": "Planetary Computer",
            "warning": "NDVI could not be processed for this study area right now.",
        }

# --------------------------------------------------
# EXTERNAL DATASETS: Soils, Population, Climate, Hydrology
# --------------------------------------------------

def get_aoi_centroid(geojson: dict) -> tuple:
    """Return (lat, lon) of centroid."""
    from shapely.geometry import shape
    centroid = shape(geojson["geometry"]).centroid
    return centroid.y, centroid.x

async def fetch_soil_soc(bbox: list, geojson_geom: dict) -> dict | None:
    """Fetch Soil Organic Carbon (SOC) from OpenGeoHub STAC."""
    try:
        catalog = pystac_client.Client.open("https://stac.opengeohub.org/")
        collection = "biomass.soc_esacci.l4.cpool_go_landmetric"
        # Search for items (static, single date)
        search = catalog.search(
            collections=[collection],
            bbox=bbox,
            limit=1
        )
        items = list(search.items())
        if not items:
            print("⚠️ No SOC items found", file=sys.stderr)
            return None

        item = items[0]
        # Get COG asset (the one without qml/sld)
        cog_key = [k for k in item.assets.keys() if k.endswith('_go_epsg4326') or k.endswith('.tif')][0]
        href = item.assets[cog_key].href

        # Load with rasterio (public S3)
        with rio.open(href) as src:
            clipped, _ = mask(
                src,
                [geojson_geom],
                crop=True,
                nodata=src.nodata,
                all_touched=True
            )
            arr = clipped[0].astype(float)
            arr[arr == src.nodata] = np.nan
            valid = arr[~np.isnan(arr)]
            if valid.size == 0:
                return None
            mean_soc = float(np.mean(valid))
            return {
                "mean_soc_tC_ha": round(mean_soc, 2),
                "units": "tC/ha",
                "source": "OpenGeoHub",
                "collection": collection,
                "date": item.datetime.isoformat() if hasattr(item.datetime, "isoformat") else str(item.datetime),
            }
    except Exception as e:
        print(f"⚠️ SOC fetch failed: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        return None

async def fetch_population(bbox: list, geojson_geom: dict) -> dict | None:
    """Fetch population from GHS-POP via OpenGeoHub STAC."""
    try:
        catalog = pystac_client.Client.open("https://stac.opengeohub.org/")
        collection = "pop.count_ghs_go_landmetric"
        # Get items, pick most recent
        search = catalog.search(
            collections=[collection],
            bbox=bbox,
            limit=10  # get multiple years, sort later
        )
        items = list(search.items())
        if not items:
            print("⚠️ No POP items found", file=sys.stderr)
            return None

        # Pick latest datetime
        latest = max(items, key=lambda it: it.datetime)
        cog_key = [k for k in latest.assets.keys() if k.startswith('pop.') and not k.endswith('qml')][0]
        href = latest.assets[cog_key].href

        with rio.open(href) as src:
            clipped, _ = mask(
                src,
                [geojson_geom],
                crop=True,
                nodata=src.nodata,
                all_touched=True
            )
            arr = clipped[0].astype(float)
            arr[arr == src.nodata] = np.nan
            valid = arr[~np.isnan(arr)]
            total_pop = float(np.nansum(valid))
            # Compute area of AOI in km²
            from shapely.geometry import shape
            area_km2 = shape(geojson_geom).area * (111.32**2)  # approximate degrees to km²
            density = total_pop / area_km2 if area_km2 > 0 else None
            return {
                "total_pop": int(round(total_pop)),
                "density_per_km2": round(density, 1) if density else None,
                "year": latest.datetime.year if hasattr(latest.datetime, "year") else None,
                "source": "OpenGeoHub",
                "collection": collection,
            }
    except Exception as e:
        print(f"⚠️ Population fetch failed: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        return None

async def fetch_climate(geojson_geom: dict) -> dict | None:
    """Fetch climate normals from Open-Meteo."""
    try:
        lat, lon = get_aoi_centroid(geojson_geom)
        url = (
            "https://climate-api.open-meteo.com/v1/climate"
            f"?latitude={lat}&longitude={lon}"
            "&start_date=1991-01-01&end_date=2020-12-31"
            "&daily=temperature_2m_mean,precipitation_sum"
        )
        resp = await asyncio.to_thread(requests.get, url, timeout=15)
        if resp.status_code != 200:
            print(f"⚠️ Open-Meteo returned {resp.status_code}", file=sys.stderr)
            return None
        data = resp.json()
        daily = data.get("daily", {})
        temps = [t for t in daily.get("temperature_2m_mean", []) if t is not None]
        precips = [p for p in daily.get("precipitation_sum", []) if p is not None]
        if not temps or not precips:
            return None
        mean_temp = float(np.mean(temps))
        annual_precip = float(np.sum(precips)) / (len(precips) / 365.25)  # per year
        return {
            "mean_temp_c": round(mean_temp, 1),
            "annual_precip_mm": round(annual_precip, 0),
            "period": "1991-2020",
            "source": "Open-Meteo",
        }
    except Exception as e:
        print(f"⚠️ Climate fetch failed: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        return None

async def fetch_hydrology(bbox: list, geojson_geom: dict) -> dict | None:
    """Fetch water features from OpenStreetMap via Overpass."""
    try:
        from shapely.geometry import shape
        import overpy
        minx, miny, maxx, maxy = bbox
        # Expand bbox slightly to catch features on edges
        buffer = 0.001  # ~100m
        minx -= buffer; miny -= buffer; maxx += buffer; maxy += buffer

        query = f"""
        [out:json][timeout:25];
        (
          way["natural"="water"](bbox:{miny},{minx},{maxy},{maxx});
          relation["natural"="water"](bbox:{miny},{minx},{maxy},{maxx});
          way["waterway"~"^(river|stream|canal)$"](bbox:{miny},{minx},{maxy},{maxx});
        );
        out body;
        >;
        out skel qt;
        """

        api = overpy.Overpass()
        result = await asyncio.to_thread(api.query, query)

        # Calculate water area (polygons) and waterway length (lines)
        water_area_m2 = 0.0
        waterway_length_km = 0.0
        aoi_geom = shape(geojson_geom)

        for elem in result.ways + result.relations:
            tags = elem.tags
            # Build shapely geometry from nodes (simplified: use OSM polygon if available)
            # For MVP, use is_polygon flag
            if hasattr(elem, "geometry") and elem.geometry:
                try:
                    from shapely import wkt
                    geom = wkt.loads(elem.geometry)
                except Exception:
                    geom = None
                if geom and not geom.is_empty:
                    if geom.area > 0 and "natural" in tags and tags["natural"] == "water":
                        # Estimate area in WGS84 degrees -> m² (rough conversion)
                        area_deg2 = geom.area
                        # Approximate: 1 deg ≈ 111km, so m² = area_deg2 * (111320)^2
                        area_m2 = area_deg2 * (111320.0**2)
                        water_area_m2 += area_m2
                    elif geom.length > 0 and "waterway" in tags:
                        length_deg = geom.length
                        length_km = length_deg * 111.32  # rough
                        waterway_length_km += length_km

        water_area_km2 = water_area_m2 / 1e6
        water_cover_pct = (water_area_km2 / (aoi_geom.area * (111.32**2))) * 100 if aoi_geom.area > 0 else 0

        return {
            "water_area_km2": round(water_area_km2, 3),
            "water_cover_pct": round(water_cover_pct, 1),
            "waterway_length_km": round(waterway_length_km, 1),
            "source": "OpenStreetMap",
        }
    except Exception as e:
        print(f"⚠️ Hydrology fetch failed: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        return None

async def get_country_from_centroid(geojson_geom: dict) -> Optional[str]:
    """Return country name using Nominatim."""
    try:
        from shapely.geometry import shape
        centroid = shape(geojson_geom).centroid
        lat, lon = centroid.y, centroid.x

        resp = await asyncio.to_thread(
            requests.get,
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "format": "json",
                "lat": lat,
                "lon": lon,
                "zoom": 4,
                "addressdetails": 1,
            },
            headers={"User-Agent": "GeoContextualize/1.0"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("address", {}).get("country")
    except Exception as e:
        print(f"⚠️ Country lookup failed: {e}", file=sys.stderr)
    return None

# --------------------------------------------------
# GEMINI
# --------------------------------------------------
def load_prompt_template(name: str) -> str:
    path = os.path.join("prompts", name)
    with open(path, "r") as f:
        return f.read()

def generate_study_area_narrative(
    summary: Dict[str, Any],
    audience: Literal["academic", "investor", "farmer", "policy"] = "academic",
) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "AI narrative generation unavailable: API key not configured"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    # Build country/regulatory context
    country = summary.get("country", "Unknown")
    admin1 = summary.get("admin_level1") or ""
    admin2 = summary.get("admin_level2") or ""
    reg_fw = summary.get("regulatory_framework")
    parts = [f"Country: {country}"]
    if admin1:
        parts.append(f"Primary administrative region: {admin1}")
    if admin2:
        parts.append(f"Secondary region: {admin2}")
    if reg_fw:
        parts.append(f"Relevant EIA framework: {reg_fw}")
    else:
        parts.append("Relevant EIA framework: International best practice")
    country_context = "\n".join(parts)

    # Build citations block
    ndvi = summary.get("ndvi", {})
    pop = summary.get("population", {})
    citations = f"""Elevation: NASA NASADEM (30 m). NASA/METI/AIST/Japan Spacesystems, 2024. Accessed via Microsoft Planetary Computer (CC-BY-4.0).
Land Cover: ESA WorldCover 2021 (10 m). © ESA WorldCover project 2021, processed by VITO. CC-BY-4.0.
Vegetation: Copernicus Sentinel-2 L2A (20 m). Scene date: {ndvi.get('scene_date','')}. Scene ID: {ndvi.get('scene_id','')}. Via {ndvi.get('source','Planetary Computer')}. CC-BY-4.0.
Soils: ESA CCI Soil Organic Carbon (100 m). Year: 2021. Via OpenGeoHub STAC. CC-BY-4.0.
Climate: Open-Meteo climate normals (1991-2020). Temperature and precipitation. https://open-meteo.com. CC-BY-4.0.
Hydrology: OpenStreetMap water features (natural=water, waterway=rivers/streams). © OpenStreetMap contributors, ODbL.
Population: GHS-POP (Global Human Settlement Layer) 100 m. Year: {pop.get('year','')}. Via OpenGeoHub STAC. CC-BY-4.0."""
    if reg_fw:
        citations += f"\nRegulatory framework: {reg_fw}"

    prompt = load_prompt_template("study_area_v2.txt").format(
        summary_data=json.dumps(summary, indent=2),
        country_context=country_context,
        citations=citations,
    )

    response = model.generate_content(prompt)
    return response.text.strip()

# --------------------------------------------------
# API ENDPOINT
# --------------------------------------------------
AVAILABLE_DATASETS = {
    "dem",
    "landcover",
    "ndvi",
    "soils",
    "population",
    "climate",
    "hydrology",
}


def _requested_datasets(value: Optional[str]) -> set[str]:
    """Parse the existing comma-separated frontend selector safely."""
    if value is None:
        return {"dem", "landcover", "ndvi"}
    requested = {name.strip().lower() for name in value.split(",") if name.strip()}
    unknown = requested - AVAILABLE_DATASETS
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported datasets: {', '.join(sorted(unknown))}",
        )
    return requested


async def _find_core_assets(
    bbox: list[float],
    *,
    need_dem: bool,
    need_landcover: bool,
) -> dict[str, str]:
    """Find just the COGs requested by the user; asset signing happens on read."""
    def search() -> dict[str, str]:
        catalog = pystac_client.Client.open(STAC_URL)
        assets: dict[str, str] = {}
        if need_dem:
            dem_items = list(catalog.search(collections=["nasadem"], bbox=bbox, limit=1).items())
            if not dem_items:
                raise HTTPException(status_code=400, detail="No elevation data available for this area")
            assets["dem"] = dem_items[0].assets["elevation"].href
        if need_landcover:
            lc_items = list(catalog.search(collections=["esa-worldcover"], bbox=bbox, limit=1).items())
            if not lc_items:
                raise HTTPException(status_code=400, detail="No land-cover data available for this area")
            assets["landcover"] = lc_items[0].assets["map"].href
        return assets

    try:
        return await asyncio.to_thread(search)
    except HTTPException:
        raise
    except (KeyError, IndexError) as exc:
        raise HTTPException(status_code=502, detail="A required raster asset was unavailable") from exc


@app.post("/generate-context", response_model=ContextResponse)
async def generate_context(
    request: GeoJSONRequest,
    include_narrative: bool = False,
    audience: str = "academic",
    include_ndvi: bool = True,
    regulatory_framework: Optional[str] = None,
    datasets: Optional[str] = None,
):
    try:
        requested = _requested_datasets(datasets)
        aoi = validate_aoi(request.geojson)
        geojson = aoi["feature"]
        geom = geojson["geometry"]
        bbox = aoi["bbox"]

        core_assets = await asyncio.wait_for(
            _find_core_assets(
                bbox,
                need_dem="dem" in requested,
                need_landcover="landcover" in requested,
            ),
            timeout=15.0,
        )

        raster_tasks: dict[str, Any] = {}
        if "dem" in core_assets:
            raster_tasks["dem"] = asyncio.to_thread(compute_raster_stats, core_assets["dem"], geojson)
        if "landcover" in core_assets:
            raster_tasks["landcover"] = asyncio.to_thread(
                compute_landcover_percentages,
                core_assets["landcover"],
                geojson,
            )
        try:
            raster_values = await asyncio.wait_for(
                asyncio.gather(*raster_tasks.values()),
                timeout=30.0,
            ) if raster_tasks else []
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Raster processing timed out; try a smaller area")
        except MemoryError:
            raise HTTPException(status_code=507, detail="Raster processing exceeded available memory")
        raster_results = dict(zip(raster_tasks.keys(), raster_values))
        dem = interpret_terrain(raster_results["dem"]) if "dem" in raster_results else None
        landcover = raster_results.get("landcover")

        ndvi_stats = None
        if include_ndvi and "ndvi" in requested:
            ndvi_stats = await compute_median_ndvi(
                bbox=bbox,
                geojson_geom=geom,
                max_area_km2=MAX_NDVI_BBOX_KM2,
                max_scenes=MAX_PC_SCENES,
                resolution_m=20,
            )

        extra_tasks: dict[str, Any] = {}
        if "soils" in requested:
            extra_tasks["soils"] = fetch_soil_soc(bbox, geom)
        if "population" in requested:
            extra_tasks["population"] = fetch_population(bbox, geom)
        if "climate" in requested:
            extra_tasks["climate"] = fetch_climate(geom)
        if "hydrology" in requested:
            extra_tasks["hydrology"] = fetch_hydrology(bbox, geom)
        try:
            extra_values = await asyncio.wait_for(
                asyncio.gather(*extra_tasks.values()),
                timeout=30.0,
            ) if extra_tasks else []
        except asyncio.TimeoutError:
            print("⚠️ Extra datasets timed out; returning the completed core analysis", file=sys.stderr)
            extra_values = [None] * len(extra_tasks)
        extra_results = dict(zip(extra_tasks.keys(), extra_values))

        # Get country
        country = await get_country_from_centroid(geom)

        # Extract scene metadata for citations
        scene_dates = {}
        scene_ids = {}
        if ndvi_stats:
            if "scene_date" in ndvi_stats:
                scene_dates["ndvi"] = ndvi_stats["scene_date"]
            if "scene_id" in ndvi_stats:
                scene_ids["ndvi"] = ndvi_stats["scene_id"]
            # Could also add from composite if multiple scenes
            if "scene_dates" in ndvi_stats:
                scene_dates["ndvi_composite"] = ", ".join(ndvi_stats["scene_dates"])
            if "scene_ids" in ndvi_stats:
                scene_ids["ndvi_composite"] = ", ".join(ndvi_stats["scene_ids"])

        summary = {
            "dem": dem,
            "ndvi": ndvi_stats,
            "landcover": landcover,
            "soils": extra_results.get("soils"),
            "population": extra_results.get("population"),
            "climate": extra_results.get("climate"),
            "hydrology": extra_results.get("hydrology"),
            "country": country,
            "admin_level1": "",  # TODO: fetch from OSM
            "admin_level2": "",  # TODO: fetch from OSM
            "regulatory_framework": regulatory_framework or "",
            "scene_dates": scene_dates,
            "scene_ids": scene_ids,
            "analysis": {
                "bbox_area_km2": round(aoi["bbox_area_km2"], 2),
                "datasets": sorted(requested),
                "mode": "synchronous",
            },
        }

        result = {"summary": summary}

        if include_narrative:
            try:
                narrative = await asyncio.wait_for(
                    asyncio.to_thread(generate_study_area_narrative, summary, audience),
                    timeout=10.0
                )
                result["narrative"] = narrative
            except asyncio.TimeoutError:
                result["narrative"] = "Narrative generation timed out (free tier limit)."
            except Exception as e:
                print(f"Narrative generation error: {str(e)}", file=sys.stderr)
                result["narrative"] = f"Narrative generation failed: {str(e)[:100]}"

        return result

    except HTTPException:
        raise
    except Exception as e:
        print(f"CRITICAL ERROR in /generate-context: {type(e).__name__} - {str(e)[:200]}", file=sys.stderr)
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)[:150]}")

# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "GeoContext Generator API",
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "version": "1.2.0"
    }

@app.get("/version")
async def get_version():
    return {
        "version": "1.2.0",
        "optimizations": [
            "validated_multipolygon_aoi",
            "bounded_planetary_computer_search",
            "clip_before_ndvi_reduction",
            "synchronous_capacity_guardrails",
        ],
        "max_sync_bbox_km2": MAX_SYNC_BBOX_KM2,
        "max_ndvi_bbox_km2": MAX_NDVI_BBOX_KM2,
        "max_pc_scenes": MAX_PC_SCENES,
        "ndvi_resolution_m": 20,
        "large_area_mode": "not available until a durable asynchronous worker is deployed",
    }
