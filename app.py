"""Knoxify — Flask entry point.

Real-world areas → Project Zomboid maps.

Run:
    source .venv/bin/activate
    python app.py

Then open http://127.0.0.1:5000/ in a browser.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import zipfile
from pathlib import Path

import requests
from flask import (Flask, jsonify, render_template, request,
                   send_from_directory)

from generator import osm, packs, renderer
from generator.jobs import Busy, Job, JobManager

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
jobs = JobManager()
pack_jobs = JobManager()
pack_manager = packs.PackManager()

MIN_METERS_PER_TILE = 0.5
MAX_METERS_PER_TILE = 4.0
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_CACHE_DIR = OUTPUT_DIR / "_cache" / "geocode"

SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search_places():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"error": "Search query is too short."}), 400

    try:
        limit = int(request.args.get("limit", 5))
    except ValueError:
        limit = 5
    limit = max(1, min(limit, 10))

    try:
        results = _search_places(query, limit)
    except Exception as exc:
        return jsonify({"error": f"Location search failed: {exc}"}), 502

    return jsonify({"results": results})


@app.route("/api/packs/status")
def pack_status():
    try:
        south, west, north, east = _request_bbox(request.args)
        result = pack_manager.status_for_bbox(south, west, north, east)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Regional data check failed: {exc}"}), 502
    return jsonify(result)


@app.route("/api/packs")
def installed_packs():
    return _no_store_json({"installed": pack_manager.list_installed()})


@app.route("/api/packs/install", methods=["POST"])
def install_pack():
    data = request.get_json(force=True) or {}
    region_id = data.get("regionId") if isinstance(data, dict) else None
    if not isinstance(region_id, str) or not region_id:
        return jsonify({"error": "Choose a regional data pack first."}), 400
    if _active_job(jobs):
        return jsonify({"error": "Wait for the current map export to finish or cancel it first."}), 409
    try:
        job = pack_jobs.start(lambda active: pack_manager.install(
            region_id, active, refresh=bool(data.get("refresh"))))
    except Busy as exc:
        return jsonify({"error": "A regional download is already running.",
                        "jobId": exc.job_id}), 409
    return jsonify(job.snapshot()), 202


@app.route("/api/packs/jobs/active")
def active_pack_job():
    with pack_jobs.lock:
        active = next((job for job in pack_jobs.jobs.values() if not job.done.is_set()), None)
    return _no_store_json(active.snapshot() if active else {})


@app.route("/api/packs/jobs/<job_id>")
def pack_job_status(job_id):
    job = pack_jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Regional download no longer available. The server may have restarted."}), 404
    return _no_store_json(job.snapshot())


@app.route("/api/packs/jobs/<job_id>/cancel", methods=["POST"])
def cancel_pack_job(job_id):
    job = pack_jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Regional download not found."}), 404
    job.cancel()
    return jsonify(job.snapshot())


@app.route("/api/packs/remove", methods=["POST"])
def remove_pack():
    data = request.get_json(force=True) or {}
    region_id = data.get("regionId") if isinstance(data, dict) else None
    if not isinstance(region_id, str) or not region_id:
        return jsonify({"error": "Regional data pack not specified."}), 400
    if _active_job(pack_jobs) or _active_job(jobs):
        return jsonify({"error": "Wait for active work to finish or cancel it first."}), 409
    return jsonify({"removed": pack_manager.remove(region_id)})


@app.route("/api/generate", methods=["POST"])
@app.route("/api/jobs", methods=["POST"])
def generate():
    data = request.get_json(force=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a map selection."}), 400
    try:
        south = float(data["south"])
        west = float(data["west"])
        north = float(data["north"])
        east = float(data["east"])
        meters_per_tile = float(data.get("metersPerTile", 1.0))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid bbox / scale."}), 400

    if not all(math.isfinite(v) for v in (south, west, north, east, meters_per_tile)):
        return jsonify({"error": "Coordinates and scale must be finite numbers."}), 400
    if not (-90 < south < north < 90 and -180 <= west < east <= 180):
        return jsonify({"error": "Select valid latitude and longitude bounds."}), 400
    if not (MIN_METERS_PER_TILE <= meters_per_tile <= MAX_METERS_PER_TILE):
        return jsonify({"error": "Scale out of allowed range."}), 400

    raw_name = data.get("mapName") or f"knoxify_{int(time.time())}"
    if not isinstance(raw_name, str):
        return jsonify({"error": "Map name must be text."}), 400
    map_name = SAFE_NAME.sub("_", raw_name).strip("_")[:80] or f"knoxify_{int(time.time())}"
    if _active_job(pack_jobs):
        return jsonify({"error": "Wait for the regional download to finish or cancel it first."}), 409
    try:
        job = jobs.start(lambda active: _generate_map(
            active, south, west, north, east, meters_per_tile, map_name))
    except Busy as exc:
        return jsonify({"error": "A map is already generating.", "jobId": exc.job_id}), 409

    if request.path == "/api/jobs":
        return jsonify(job.snapshot()), 202
    # Preserve the original synchronous API for existing scripts.
    job.done.wait()
    state = job.snapshot()
    if state["state"] == "complete":
        return jsonify(state["result"])
    return jsonify({"error": state["error"] or state["message"]}), 502


@app.route("/api/jobs/active")
def active_job():
    with jobs.lock:
        active = next((job for job in jobs.jobs.values() if not job.done.is_set()), None)
    response = jsonify(active.snapshot() if active else {})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Export no longer available. The server may have restarted."}), 404
    response = jsonify(job.snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Export not found."}), 404
    job.cancel()
    return jsonify(job.snapshot())


def _generate_map(job: Job, south, west, north, east, meters_per_tile, map_name):
    started = time.monotonic()

    fetch_stats = osm.FetchStats()
    local_pack = pack_manager.find_installed(south, west, north, east)
    try:
        if local_pack:
            features = pack_manager.fetch_features(
                local_pack, south, west, north, east, stats=fetch_stats,
                check_cancel=job.check,
                progress=lambda fraction, message: job.update(
                    fraction * 55, message, "fetching", fetch=fetch_stats.as_dict()))
        else:
            features = osm.fetch_features(south, west, north, east,
                stats=fetch_stats, check_cancel=job.check,
                progress=lambda fraction, message: job.update(
                    fraction * 55, message, "fetching", fetch=fetch_stats.as_dict()))
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Map data failed: {exc}") from exc
    fetched_at = time.monotonic()
    job.check()

    map_dir = OUTPUT_DIR / map_name
    if map_dir.exists():
        map_name = f"{map_name}_{job.id[:8]}"
        map_dir = OUTPUT_DIR / map_name
    map_dir.mkdir(parents=True, exist_ok=False)
    source_meta = {
        "feature_count": len(features),
        "map_data": fetch_stats.as_dict(),
    }
    if fetch_stats.source == "online":
        source_meta["overpass"] = fetch_stats.as_dict()
    result = renderer.render(
        features, south, west, north, east,
        meters_per_tile=meters_per_tile,
        output_dir=str(map_dir),
        map_name=map_name,
        extra_meta=source_meta,
        check_cancel=job.check,
        progress=lambda fraction, message: job.update(55 + fraction * 39, message, "rendering"),
    )
    rendered_at = time.monotonic()

    _write_readme(map_dir, map_name, result, len(features), fetch_stats)
    _write_zip(map_dir, map_name, job)
    timings = {
        "fetch": round(fetched_at - started, 2),
        "render": round(rendered_at - fetched_at, 2),
        "package": round(time.monotonic() - rendered_at, 2),
        "total": round(time.monotonic() - started, 2),
    }
    if Path(result.meta_path).exists():
        metadata = json.loads(Path(result.meta_path).read_text(encoding="utf-8"))
        metadata["timings_seconds"] = timings
        Path(result.meta_path).write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return {
        "mapName": map_name,
        "width": result.width,
        "height": result.height,
        "cellsX": result.cells_x,
        "cellsY": result.cells_y,
        "featureCount": len(features),
        "osmSeconds": round(fetch_stats.seconds, 2),
        "totalSeconds": timings["total"],
        "timings": timings,
        "fetch": fetch_stats.as_dict(),
        "files": {
            "landscape": f"/output/{map_name}/{Path(result.landscape_path).name}",
            "vegetation": f"/output/{map_name}/{Path(result.vegetation_path).name}",
            "spawn": f"/output/{map_name}/{Path(result.spawn_map_path).name}",
            "preview": f"/output/{map_name}/{Path(result.preview_path).name}",
            "buildings": f"/output/{map_name}/{Path(result.buildings_geojson_path).name}",
            "meta": f"/output/{map_name}/{Path(result.meta_path).name}",
            "zip": f"/output/{map_name}/{map_name}.zip",
            "readme": f"/output/{map_name}/README.txt",
        },
    }


@app.route("/output/<path:relpath>")
def serve_output(relpath: str):
    return send_from_directory(OUTPUT_DIR, relpath)


def _request_bbox(values) -> tuple[float, float, float, float]:
    try:
        south, west, north, east = (float(values[name]) for name in
                                    ("south", "west", "north", "east"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Missing or invalid map bounds.") from exc
    if not all(math.isfinite(value) for value in (south, west, north, east)):
        raise ValueError("Map bounds must be finite numbers.")
    if not (-90 < south < north < 90 and -180 <= west < east <= 180):
        raise ValueError("Select valid latitude and longitude bounds.")
    return south, west, north, east


def _no_store_json(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def _active_job(manager: JobManager) -> Job | None:
    with manager.lock:
        return next((job for job in manager.jobs.values() if not job.done.is_set()), None)


def _search_places(query: str, limit: int) -> list[dict]:
    cache_key = hashlib.sha1(
        json.dumps({"q": query.lower(), "limit": limit},
                   sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_path = GEOCODE_CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    params = {
        "q": query,
        "format": "jsonv2",
        "limit": str(limit),
        "addressdetails": "0",
    }
    headers = {"User-Agent": osm.USER_AGENT}
    response = requests.get(NOMINATIM_SEARCH_URL, params=params,
                            headers=headers, timeout=15)
    if response.status_code == 429:
        raise RuntimeError("Nominatim rate limit reached. Try again later.")
    response.raise_for_status()

    results = []
    for item in response.json():
        bbox = _parse_nominatim_bbox(item)
        if bbox is None:
            continue
        results.append({
            "displayName": item.get("display_name", "Unnamed place"),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "south": bbox[0],
            "west": bbox[1],
            "north": bbox[2],
            "east": bbox[3],
            "category": item.get("category") or item.get("class"),
            "type": item.get("type"),
            "importance": item.get("importance"),
        })

    GEOCODE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(results), encoding="utf-8")
    return results


def _parse_nominatim_bbox(item: dict) -> tuple[float, float, float, float] | None:
    bbox = item.get("boundingbox")
    try:
        if bbox and len(bbox) == 4:
            south, north, west, east = (float(v) for v in bbox)
            if south < north and west < east:
                return south, west, north, east
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    delta = 0.003
    return lat - delta, lon - delta, lat + delta, lon + delta


def _write_readme(map_dir: Path, map_name: str, result: renderer.RenderResult,
                  feature_count: int, fetch_stats: osm.FetchStats) -> None:
    source_text = (f"Offline pack:     {fetch_stats.pack_name}"
                   if fetch_stats.source == "offline" else
                   f"Overpass chunks:   {fetch_stats.chunks_total} total, "
                   f"{fetch_stats.chunks_from_cache} from cache")
    text = f"""Project Zomboid map: {map_name}
Generated by Knoxify.

Bitmap dimensions: {result.width}x{result.height} tiles
Cell grid:         {result.cells_x} x {result.cells_y} (cells are always 300 tiles)
OSM features:      {feature_count}
{source_text}

Files
-----
{map_name}.bmp                  Landscape (base terrain — WorldEd's main input)
{map_name}_veg.bmp              Vegetation (trees, bushes, long grass)
{map_name}_ZombieSpawnMap.bmp   Zombie population (grayscale, 1/10 scale)
{map_name}_preview.png          Human-viewable preview of what you'll get
{map_name}_buildings.geojson    Building footprints from OSM (for reference)
{map_name}_info.json            Meta: bbox, scale, cell count
{map_name}.zip                  All three BMPs together for easy transfer

How to import (per Thuztor's Mapping Guide v0.2, chapter 2)
-----------------------------------------------------------
1. Open WorldEd (part of the Zomboid Mapping Tools).
2. File -> New, and choose a {result.cells_x} x {result.cells_y} cell grid.
3. From your file browser, drag {map_name}.bmp onto the empty grid.
   (WorldEd reads the matching {map_name}_veg.bmp automatically if it sits
   next to the landscape bitmap with the same base filename.)
4. File -> BMP to TMX -> All cells. Set an export folder for the .tmx output.
5. Open the resulting project in WorldEd / TileZed to place buildings
   (.tbx files) on top of the landscape. This tool does NOT place PZ
   buildings — OSM building footprints are exported as GeoJSON for
   reference only.
6. File -> Generate Lots. This produces .lotheader + .lotpack files.
7. Copy those into your game's media/maps folder (see chapter 9 of the
   guide for offset / world-origin details).

Notes
-----
* Roads render as asphalt (residential = light, secondary = medium,
  primary/motorway = dark). Paths and tracks render as dirt lines.
* Forests render as dense trees in the interior and a grass+tree blend
  at the edges so the transition isn't a hard rectangle.
* The spawn map is generated procedurally: higher density on asphalt,
  zero on water, slight randomness throughout.
* OSM building footprints CANNOT be converted directly to PZ buildings —
  PZ buildings are a separate thing you assemble in BuildingEd (.tbx).
  The GeoJSON file lets you see where buildings would sit in the real
  world and drop matching .tbx lots in the right tiles.
"""
    (map_dir / "README.txt").write_text(text)


def _write_zip(map_dir: Path, map_name: str, job: Job) -> None:
    zip_path = map_dir / f"{map_name}.zip"
    paths = [map_dir / name for name in (f"{map_name}.bmp", f"{map_name}_veg.bmp",
                                        f"{map_name}_ZombieSpawnMap.bmp", "README.txt")]
    paths = [path for path in paths if path.exists()]
    total = max(1, sum(path.stat().st_size for path in paths))
    written = 0
    # A partial/cancelled archive is never advertised as a finished ZIP.
    partial = zip_path.with_suffix(".zip.part")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for path in paths:
            with path.open("rb") as source, zf.open(path.name, "w", force_zip64=True) as target:
                while block := source.read(1024 * 1024):
                    job.check()
                    target.write(block)
                    written += len(block)
                    job.update(94 + 5 * written / total, "Preparing download", "packaging")
    job.check()
    partial.replace(zip_path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
