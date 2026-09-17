"""GeoContextualize API.

The live endpoint deliberately keeps analyses small and bounded. Large or
long-running areas need a durable job queue and object storage before they can
be supported safely; this synchronous API rejects them instead of attempting a
best-effort process that can exhaust the server or Planetary Computer.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from collections import Counter
from itertools import islice
from typing import Any, Dict, Literal, Optional

import google.generativeai as genai
import numpy as np
import planetary_computer
import pystac_client
import rasterio as rio
import rioxarray  # Registers the rio xarray accessor used below.
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from odc.stac import load as stac_load
from pydantic import BaseModel
from pyproj import Geod
from rasterio.mask import mask
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity


load_dotenv()


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive integer setting without making a bad environment fatal."""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_float(name: str, default: float, minimum: float = 0.01) -> float:
    """Read a positive float setting without making a bad environment fatal."""
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _cors_origins() -> list[str]:
    raw = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    origins = [
        origin.strip().rstrip("/")
        for origin in raw.split(",")
        if origin.strip() and origin.strip() != "*"
    ]
    return origins or ["http://localhost:3000", "http://127.0.0.1:3000"]


STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
GEOD = Geod(ellps="WGS84")

# These values are deliberately conservative for a 2-vCPU / 4-GB server.
# Both DEM/land-cover and Sentinel-2 currently load by bounding box, so the
# bounding-box cap (not only the polygon's area) is the safety boundary.
MAX_GEOJSON_BYTES = _positive_int("MAX_GEOJSON_BYTES", 500_000)
MAX_AOI_VERTICES = _positive_int("MAX_AOI_VERTICES", 10_000)
MAX_SYNC_BBOX_KM2 = _positive_float("MAX_SYNC_BBOX_KM2", 100.0)
MAX_NDVI_BBOX_KM2 = _positive_float("MAX_NDVI_BBOX_KM2", 10.0)
MAX_PC_SCENES = _positive_int("MAX_PC_SCENES", 4)
MAX_CONCURRENT_ANALYSES = _positive_int("MAX_CONCURRENT_ANALYSES", 1)
STAC_RETRIES = _positive_int("STAC_RETRIES", 2)
NDVI_TIMEOUT_SECONDS = _positive_float("NDVI_TIMEOUT_SECONDS", 90.0, 5.0)

analysis_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)

app = FastAPI(title="GeoContext Generator API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GeoJSONRequest(BaseModel):
    geojson: Dict[str, Any]


class ContextResponse(BaseModel):
    summary: Dict[str, Any]
    narrative: Optional[str] = None


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


def _count_coordinate_positions(value: Any) -> int:
    """Count GeoJSON coordinate positions before Shapely processes a payload."""
    if isinstance(value, (list, tuple)):
        if (
            len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            return 1
        return sum(_count_coordinate_positions(child) for child in value)
    return 0


def _feature_geometry(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if candidate.get("type") != "Feature":
        raise HTTPException(status_code=400, detail="FeatureCollection entries must be GeoJSON Features")
    geometry = candidate.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(status_code=400, detail="Each feature must include a geometry")
    return geometry


def normalize_geojson(geojson: Dict[str, Any]) -> Dict[str, Any]:
    """Return one validated Polygon or MultiPolygon Feature.

    FeatureCollections are unioned rather than silently using their first
    feature. This guarantees that all selected polygon parts participate in
    clipping and that bounds are calculated across a MultiPolygon safely.
    """
    if not isinstance(geojson, dict):
        raise HTTPException(status_code=400, detail="GeoJSON must be an object")

    try:
        serialized = json.dumps(geojson, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="GeoJSON must be JSON serializable") from exc

    if len(serialized.encode("utf-8")) > MAX_GEOJSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"GeoJSON exceeds the {MAX_GEOJSON_BYTES:,}-byte request limit",
        )

    geojson_type = geojson.get("type")
    if geojson_type == "FeatureCollection":
        features = geojson.get("features")
        if not isinstance(features, list) or not features:
            raise HTTPException(status_code=400, detail="FeatureCollection must contain at least one feature")
        geometries = [_feature_geometry(feature) for feature in features if isinstance(feature, dict)]
        if len(geometries) != len(features):
            raise HTTPException(status_code=400, detail="FeatureCollection entries must be GeoJSON Features")
    elif geojson_type == "Feature":
        geometries = [_feature_geometry(geojson)]
    elif geojson_type in {"Polygon", "MultiPolygon"}:
        geometries = [geojson]
    else:
        raise HTTPException(
            status_code=400,
            detail="Only Polygon, MultiPolygon, Feature, and FeatureCollection GeoJSON are supported",
        )

    vertex_count = sum(_count_coordinate_positions(geometry.get("coordinates")) for geometry in geometries)
    if vertex_count == 0:
        raise HTTPException(status_code=400, detail="GeoJSON has no polygon coordinates")
    if vertex_count > MAX_AOI_VERTICES:
        raise HTTPException(
            status_code=413,
            detail=f"GeoJSON exceeds the {MAX_AOI_VERTICES:,}-vertex limit",
        )

    parsed_geometries = []
    for geometry in geometries:
        if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise HTTPException(
                status_code=400,
                detail="Only Polygon and MultiPolygon geometries can be analysed",
            )
        try:
            parsed = shape(geometry)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="GeoJSON geometry could not be parsed") from exc
        if parsed.is_empty:
            raise HTTPException(status_code=400, detail="GeoJSON geometry is empty")
        if not parsed.is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid polygon geometry: {explain_validity(parsed)}",
            )
        parsed_geometries.append(parsed)

    geometry = unary_union(parsed_geometries)
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(status_code=400, detail="The selected polygons do not form a valid analysis area")
    if not geometry.is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid polygon geometry: {explain_validity(geometry)}",
        )

    minx, miny, maxx, maxy = geometry.bounds
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise HTTPException(status_code=400, detail="GeoJSON coordinates must use WGS84 longitude/latitude bounds")
    if minx >= maxx or miny >= maxy:
        raise HTTPException(status_code=400, detail="GeoJSON must enclose a non-zero area")

    return {
        "type": "Feature",
        "properties": {},
        "geometry": mapping(geometry),
    }


def geometry_area_km2(geometry: Dict[str, Any]) -> float:
    """Calculate geodesic area instead of relying on degrees-to-km shortcuts."""
    area_m2, _ = GEOD.geometry_area_perimeter(shape(geometry))
    return abs(area_m2) / 1_000_000


def aoi_bounds(geojson: Dict[str, Any]) -> tuple[list[float], float, float]:
    geometry = geojson["geometry"]
    minx, miny, maxx, maxy = shape(geometry).bounds
    bbox_geometry = {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny],
            [maxx, miny],
            [maxx, maxy],
            [minx, maxy],
            [minx, miny],
        ]],
    }
    return [minx, miny, maxx, maxy], geometry_area_km2(geometry), geometry_area_km2(bbox_geometry)


def compute_raster_stats(asset_href: str, geojson: Dict[str, Any]) -> Dict[str, Any]:
    try:
        signed_url = planetary_computer.sign(asset_href)
        with rio.open(signed_url) as src:
            clipped, _ = mask(src, [geojson["geometry"]], crop=True, filled=False)
            values = np.ma.filled(clipped[0].astype(float), np.nan)
            values = values[np.isfinite(values)]
            if values.size == 0:
                return {"error": "No valid elevation pixels"}
            return {
                "mean": float(np.mean(values)),
                "min": float(np.min(values)),
                "max": float(np.max(values)),
                "std": float(np.std(values)),
                "source": "NASA DEM via Microsoft Planetary Computer",
            }
    except Exception as exc:
        print(f"DEM computation error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"error": "Elevation data could not be processed"}


def interpret_terrain(dem: Dict[str, Any]) -> Dict[str, Any]:
    if "mean" not in dem:
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


def compute_landcover_percentages(asset_href: str, geojson: Dict[str, Any]) -> Dict[str, Any]:
    try:
        signed_url = planetary_computer.sign(asset_href)
        with rio.open(signed_url) as src:
            clipped, _ = mask(src, [geojson["geometry"]], crop=True, filled=False)
            values = np.ma.compressed(clipped[0])
            if values.size == 0:
                return {"error": "No valid land-cover pixels"}

            counts = Counter(values.astype(int).flatten())
            total = values.size
            percentages = {str(key): round(value / total * 100, 2) for key, value in counts.items()}
            labeled = label_landcover(percentages)
            dominant_class = max(labeled, key=labeled.get)
            return {
                "classes": labeled,
                "dominant_class": dominant_class,
                "dominant_percentage": labeled[dominant_class],
                "source": "ESA WorldCover via Microsoft Planetary Computer",
            }
    except Exception as exc:
        print(f"Land-cover computation error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"error": "Land-cover data could not be processed"}


def _search_stac_items(
    collections: list[str],
    bbox: list[float],
    max_items: int,
    time_window: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
) -> list[Any]:
    """Fetch at most max_items items; do not materialize an unbounded search."""
    catalog = pystac_client.Client.open(STAC_URL)
    search_options: Dict[str, Any] = {
        "collections": collections,
        "bbox": bbox,
        "limit": max_items,
        "max_items": max_items,
    }
    if time_window:
        search_options["datetime"] = time_window
    if query:
        search_options["query"] = query
    search = catalog.search(**search_options)
    return list(islice(search.items(), max_items))


async def search_stac_items(
    collections: list[str],
    bbox: list[float],
    max_items: int,
    time_window: Optional[str] = None,
    query: Optional[Dict[str, Any]] = None,
) -> list[Any]:
    """Retry short transient catalogue failures without multiplying query size."""
    last_error: Optional[Exception] = None
    for attempt in range(STAC_RETRIES):
        try:
            return await asyncio.to_thread(
                _search_stac_items,
                collections,
                bbox,
                max_items,
                time_window,
                query,
            )
        except Exception as exc:
            last_error = exc
            if attempt + 1 < STAC_RETRIES:
                await asyncio.sleep(0.5 * (2**attempt))
    raise RuntimeError("Planetary Computer catalogue is temporarily unavailable") from last_error


def _median_ndvi_from_items(
    items: list[Any],
    bbox: list[float],
    geojson_geometry: Dict[str, Any],
    resolution_m: int,
) -> Dict[str, Any]:
    """Compute a clipped Sentinel-2 median composite in a worker thread."""
    resolution_degrees = resolution_m / 111_320
    data = stac_load(
        items,
        bands=["B04", "B08", "SCL"],
        crs="EPSG:4326",
        resolution=resolution_degrees,
        chunks={"x": 512, "y": 512},
        patch_url=planetary_computer.sign,
        bbox=bbox,
        dtype="uint16",
        groupby="solar_day",
        skip_broken=True,
    )

    valid_scl = data.SCL.isin([4, 5, 6, 7, 11])
    # Sentinel-2 reflectance is loaded as uint16. Cast before subtraction so
    # pixels with B08 < B04 produce negative NDVI instead of unsigned underflow.
    red = data.B04.where(valid_scl).astype("float32")
    nir = data.B08.where(valid_scl).astype("float32")
    ndvi = (nir - red) / (nir + red + 1e-8)
    ndvi = ndvi.rio.write_crs("EPSG:4326")
    clipped_ndvi = ndvi.rio.clip([geojson_geometry], crs="EPSG:4326", all_touched=True)
    median_ndvi = (
        clipped_ndvi.median(dim="time", skipna=True)
        if "time" in clipped_ndvi.dims
        else clipped_ndvi
    )
    values = np.asarray(median_ndvi.compute().values, dtype=float)
    values = values[np.isfinite(values) & (values >= -1) & (values <= 1)]

    if values.size == 0:
        return {
            "status": "unavailable",
            "warning": "No cloud-free Sentinel-2 NDVI pixels were available for this area and time window.",
            "scene_count": len(items),
            "source": "Microsoft Planetary Computer Sentinel-2 L2A",
        }

    return {
        "status": "complete",
        "mean": float(np.mean(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "std": float(np.std(values)),
        "p25": float(np.percentile(values, 25)),
        "p75": float(np.percentile(values, 75)),
        "scene_count": len(items),
        "resolution_m": resolution_m,
        "method": "bounded_cloud_masked_median_composite",
        "source": "Microsoft Planetary Computer Sentinel-2 L2A",
    }


async def compute_median_ndvi(
    bbox: list[float],
    geojson_geometry: Dict[str, Any],
    bbox_area_km2: float,
    resolution_m: int = 20,
) -> Dict[str, Any]:
    """Return a bounded PC NDVI result, never a whole-raster fallback."""
    if bbox_area_km2 > MAX_NDVI_BBOX_KM2:
        return {
            "status": "skipped",
            "warning": (
                f"NDVI was not calculated because this analysis bounding box is "
                f"{bbox_area_km2:.1f} km²; the synchronous Sentinel-2 limit is "
                f"{MAX_NDVI_BBOX_KM2:g} km²."
            ),
            "source": "Microsoft Planetary Computer Sentinel-2 L2A",
        }

    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(days=90)
    time_window = f"{start:%Y-%m-%d}/{end:%Y-%m-%d}"
    try:
        items = await search_stac_items(
            ["sentinel-2-l2a"],
            bbox,
            MAX_PC_SCENES,
            time_window,
            {"eo:cloud_cover": {"lt": 30}},
        )
    except Exception:
        return {
            "status": "unavailable",
            "warning": "Sentinel-2 catalogue is temporarily unavailable. Please try again shortly.",
            "source": "Microsoft Planetary Computer Sentinel-2 L2A",
        }

    if not items:
        return {
            "status": "unavailable",
            "warning": "No recent cloud-filtered Sentinel-2 scenes were found for this area.",
            "source": "Microsoft Planetary Computer Sentinel-2 L2A",
        }

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_median_ndvi_from_items, items, bbox, geojson_geometry, resolution_m),
            timeout=NDVI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {
            "status": "unavailable",
            "warning": "NDVI processing exceeded the synchronous time limit. Try a smaller area.",
            "scene_count": len(items),
            "source": "Microsoft Planetary Computer Sentinel-2 L2A",
        }
    except Exception as exc:
        print(f"NDVI processing error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {
            "status": "unavailable",
            "warning": "NDVI could not be processed safely for this request. Try a smaller area.",
            "scene_count": len(items),
            "source": "Microsoft Planetary Computer Sentinel-2 L2A",
        }


def load_prompt_template(name: str) -> str:
    path = os.path.join("prompts", name)
    with open(path, encoding="utf-8") as file:
        return file.read()


def generate_study_area_narrative(
    summary: Dict[str, Any],
    audience: Literal["academic", "investor", "farmer", "policy"] = "academic",
) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "AI narrative generation unavailable: API key not configured"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = load_prompt_template("study_area_v1.txt").format(
        summary_data=json.dumps(summary, indent=2),
        audience=audience,
    )
    return model.generate_content(prompt).text.strip()


def selected_dataset_names(datasets: str, include_ndvi: bool) -> set[str]:
    allowed = {"dem", "landcover", "ndvi"}
    requested = {name.strip().lower() for name in datasets.split(",") if name.strip()}
    if not requested:
        requested = allowed
    unknown = requested - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported dataset selection: {', '.join(sorted(unknown))}")
    if not include_ndvi:
        requested.discard("ndvi")
    if not requested:
        raise HTTPException(status_code=400, detail="Select at least one dataset to analyse")
    return requested


async def _generate_context(
    request: GeoJSONRequest,
    include_narrative: bool,
    audience: str,
    include_ndvi: bool,
    datasets: str,
) -> Dict[str, Any]:
    geojson = normalize_geojson(request.geojson)
    bbox, geometry_area, bbox_area = aoi_bounds(geojson)
    if bbox_area > MAX_SYNC_BBOX_KM2:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Analysis bounding box is {bbox_area:.1f} km²; the synchronous limit is "
                f"{MAX_SYNC_BBOX_KM2:g} km². Split the area into smaller analyses."
            ),
        )

    requested = selected_dataset_names(datasets, include_ndvi)
    summary: Dict[str, Any] = {
        "analysis_area": {
            "geometry_km2": round(geometry_area, 3),
            "bounding_box_km2": round(bbox_area, 3),
            "synchronous_bounding_box_limit_km2": MAX_SYNC_BBOX_KM2,
        }
    }

    search_requests: list[tuple[str, str, str]] = []
    if "dem" in requested:
        search_requests.append(("dem", "nasadem", "elevation"))
    if "landcover" in requested:
        search_requests.append(("landcover", "esa-worldcover", "map"))

    if search_requests:
        search_results = await asyncio.gather(
            *[
                search_stac_items([collection], bbox, 1)
                for _, collection, _ in search_requests
            ],
            return_exceptions=True,
        )
        processing_tasks = []
        processing_names = []
        for (name, _, asset_name), search_result in zip(search_requests, search_results):
            if isinstance(search_result, Exception):
                summary[name] = {"error": "Data catalogue is temporarily unavailable"}
                continue
            if not search_result:
                summary[name] = {"error": "No data was available for this area"}
                continue
            asset = search_result[0].assets.get(asset_name)
            if asset is None:
                summary[name] = {"error": "The selected data item did not contain the required asset"}
                continue
            if name == "dem":
                processing_tasks.append(asyncio.to_thread(compute_raster_stats, asset.href, geojson))
            else:
                processing_tasks.append(asyncio.to_thread(compute_landcover_percentages, asset.href, geojson))
            processing_names.append(name)

        if processing_tasks:
            try:
                processed = await asyncio.wait_for(asyncio.gather(*processing_tasks), timeout=45)
            except asyncio.TimeoutError:
                processed = [{"error": "Processing exceeded the synchronous time limit"} for _ in processing_names]
            for name, processed_result in zip(processing_names, processed):
                summary[name] = interpret_terrain(processed_result) if name == "dem" else processed_result

    if "ndvi" in requested:
        summary["ndvi"] = await compute_median_ndvi(
            bbox=bbox,
            geojson_geometry=geojson["geometry"],
            bbox_area_km2=bbox_area,
        )

    result: Dict[str, Any] = {"summary": summary}
    if include_narrative:
        safe_audience: Literal["academic", "investor", "farmer", "policy"]
        safe_audience = audience if audience in {"academic", "investor", "farmer", "policy"} else "academic"
        try:
            result["narrative"] = await asyncio.wait_for(
                asyncio.to_thread(generate_study_area_narrative, summary, safe_audience),
                timeout=60,
            )
        except asyncio.TimeoutError:
            result["narrative"] = "Narrative generation timed out."
        except Exception as exc:
            print(f"Narrative generation error: {type(exc).__name__}: {exc}", file=sys.stderr)
            result["narrative"] = "Narrative generation is temporarily unavailable."
    return result


@app.post("/generate-context", response_model=ContextResponse)
async def generate_context(
    request: GeoJSONRequest,
    include_narrative: bool = False,
    audience: str = "academic",
    include_ndvi: bool = True,
    datasets: str = "dem,landcover,ndvi",
) -> Dict[str, Any]:
    """Generate a bounded, synchronous contextual summary for a valid polygon."""
    try:
        await asyncio.wait_for(analysis_semaphore.acquire(), timeout=0.1)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="The analysis service is busy. Please retry in a moment.",
            headers={"Retry-After": "15"},
        ) from exc

    try:
        return await _generate_context(request, include_narrative, audience, include_ndvi, datasets)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Unexpected generate-context error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise HTTPException(status_code=500, detail="Analysis failed unexpectedly") from exc
    finally:
        analysis_semaphore.release()


@app.get("/health")
async def health_check() -> Dict[str, str]:
    return {
        "status": "healthy",
        "service": "GeoContext Generator API",
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "version": "2.0.0",
    }


@app.get("/version")
async def get_version() -> Dict[str, Any]:
    return {
        "version": "2.0.0",
        "data_sources": {
            "dem": "NASA DEM via Microsoft Planetary Computer",
            "landcover": "ESA WorldCover via Microsoft Planetary Computer",
            "ndvi": "Sentinel-2 L2A via Microsoft Planetary Computer",
        },
        "synchronous_bbox_limit_km2": MAX_SYNC_BBOX_KM2,
        "ndvi_bbox_limit_km2": MAX_NDVI_BBOX_KM2,
        "ndvi_max_scenes": MAX_PC_SCENES,
        "large_area_jobs": "not yet supported; split large areas before submitting",
    }
