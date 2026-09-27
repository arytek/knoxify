"""Query OpenStreetMap via the Overpass API.

We only pull tags that map cleanly onto PZ terrain categories — everything
else is ignored. Large selections are split into smaller bbox queries, then
deduped back into one feature list before rasterization.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import requests

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]
if os.environ.get("KNOXIFY_OVERPASS_URL"):
    OVERPASS_ENDPOINTS = [os.environ["KNOXIFY_OVERPASS_URL"]]

USER_AGENT = "Knoxify/0.2 (Project Zomboid mapping tool)"
DEFAULT_CACHE_DIR = (
    Path(__file__).resolve().parents[1] / "output" / "_cache" / "overpass"
)
DEFAULT_CHUNK_AREA_KM2 = 12.0
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRY_SLEEP_SECONDS = 12.0

# Tag filters — each line becomes one part of the Overpass union query.
# Order doesn't matter here; the rasterizer picks priority at paint time.
OVERPASS_FILTERS: Sequence[str] = (
    # water
    'way["natural"="water"]',
    'way["waterway"]',
    'relation["natural"="water"]',
    'way["landuse"="reservoir"]',
    'way["landuse"="basin"]',
    # forest / trees
    'way["landuse"="forest"]',
    'way["natural"="wood"]',
    'relation["landuse"="forest"]',
    'relation["natural"="wood"]',
    'way["natural"="scrub"]',
    'way["natural"="heath"]',
    'node["natural"="tree"]',
    # grass / parks / farms
    'way["landuse"="grass"]',
    'way["landuse"="meadow"]',
    'way["landuse"="farmland"]',
    'way["landuse"="farmyard"]',
    'way["leisure"="park"]',
    'way["leisure"="garden"]',
    'way["leisure"="pitch"]',
    # sand / beach
    'way["natural"="beach"]',
    'way["natural"="sand"]',
    # dirt
    'way["landuse"="brownfield"]',
    'way["landuse"="construction"]',
    'way["landuse"="quarry"]',
    # roads
    'way["highway"]',
    # buildings
    'way["building"]',
    'relation["building"]',
)


@dataclass
class OSMFeature:
    osm_id: int
    kind: str             # "way" or "relation" or "node"
    tags: dict
    geometry: list        # for way: list of (lat, lon); relation: list of ring lists
    role_geoms: list = field(default_factory=list)  # relation members with roles


@dataclass
class FetchStats:
    """Runtime metadata for a fetch, filled in when passed to fetch_features."""

    chunks_total: int = 0
    chunks_from_cache: int = 0
    chunks_requested: int = 0
    http_requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    endpoints_tried: list[str] = field(default_factory=list)
    cache_dir: str = ""
    seconds: float = 0.0
    source: str = "online"
    pack_id: str = ""
    pack_name: str = ""

    def as_dict(self) -> dict:
        return {
            "chunks_total": self.chunks_total,
            "chunks_from_cache": self.chunks_from_cache,
            "chunks_requested": self.chunks_requested,
            "http_requests": self.http_requests,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "endpoints_tried": sorted(set(self.endpoints_tried)),
            "cache_dir": self.cache_dir,
            "seconds": round(self.seconds, 2),
            "source": self.source,
            "pack_id": self.pack_id,
            "pack_name": self.pack_name,
        }


def _build_query(south: float, west: float, north: float, east: float,
                 timeout: int = 60) -> str:
    bbox = f"{south},{west},{north},{east}"
    parts = [f"{f}({bbox});" for f in OVERPASS_FILTERS]
    body = "\n  ".join(parts)
    return (
        f"[out:json][timeout:{timeout}];\n"
        f"(\n  {body}\n);\n"
        f"out geom;\n"
    )


def fetch_features(south: float, west: float, north: float, east: float,
                   timeout: int = 60,
                   *,
                   stats: FetchStats | None = None,
                   use_cache: bool = True,
                   cache_dir: str | os.PathLike | None = None,
                   max_chunk_area_km2: float = DEFAULT_CHUNK_AREA_KM2,
                   progress: Callable[[float, str], None] | None = None,
                   check_cancel: Callable[[], None] | None = None,
                   ) -> list[OSMFeature]:
    """Run chunked Overpass queries, dedupe results, and cache raw payloads."""
    started = time.monotonic()
    active_stats = stats or FetchStats()
    cache_root = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    active_stats.cache_dir = str(cache_root)
    chunks = _split_bbox(south, west, north, east, max_chunk_area_km2)
    active_stats.chunks_total = len(chunks)

    seen: dict[tuple[str, int], OSMFeature] = {}
    check = check_cancel or (lambda: None)
    report = progress or (lambda fraction, message: None)
    endpoints = list(OVERPASS_ENDPOINTS)
    with requests.Session() as session:
        for index, (cs, cw, cn, ce) in enumerate(chunks):
            check()

            def notify(message):
                active_stats.seconds = time.monotonic() - started
                report(index / len(chunks), f"Area {index + 1} of {len(chunks)}: {message}")

            query = _build_query(cs, cw, cn, ce, timeout=timeout)
            payload = _fetch_payload(query, timeout, active_stats,
                                     cache_root if use_cache else None,
                                     session, endpoints, notify, check)
            check()
            for feat in _parse(payload):
                seen.setdefault((feat.kind, feat.osm_id), feat)
            active_stats.seconds = time.monotonic() - started
            report((index + 1) / len(chunks),
                   f"{index + 1} of {len(chunks)} areas ready ({active_stats.chunks_from_cache} cached)")

    active_stats.seconds = time.monotonic() - started
    return list(seen.values())


def _fetch_payload(query: str, timeout: int, stats: FetchStats,
                   cache_root: Path | None, session, endpoints: list[str],
                   notify, check) -> dict:
    cache_path = _cache_path(cache_root, query) if cache_root is not None else None
    if cache_path is not None and cache_path.exists():
        try:
            with cache_path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            _validate_payload(payload)
        except (OSError, ValueError):
            notify("refreshing incomplete cached data")
        else:
            stats.chunks_from_cache += 1
            notify("using saved map data")
            return payload

    stats.chunks_requested += 1
    last_err: Exception | None = None
    headers = {"User-Agent": USER_AGENT}
    attempts = 0
    for endpoint in list(endpoints):
        for attempt in range(2):
            check()
            notify("requesting map data" if not attempts else "retrying map data request")
            stats.endpoints_tried.append(endpoint)
            stats.http_requests += 1
            if attempts:
                stats.retries += 1
            attempts += 1
            try:
                r = session.post(endpoint, data={"data": query}, headers=headers,
                                 timeout=(10, timeout + 10))
                check()
                if r.status_code in RETRYABLE_STATUS:
                    last_err = RuntimeError(f"{endpoint} -> {r.status_code}")
                    if r.status_code == 429:
                        stats.rate_limited += 1
                        if attempt == 0:
                            _wait_for_retry(_retry_seconds(r, attempt), notify, check)
                            continue
                        # Do not rotate public servers to evade a rate limit.
                        raise RuntimeError("Map service is busy. Try again later; completed areas are saved.")
                    break
                r.raise_for_status()
                payload = r.json()
                _validate_payload(payload)
                if cache_path is not None:
                    _write_cache(cache_path, payload)
                # Subsequent chunks use the server that actually answered.
                endpoints.remove(endpoint)
                endpoints.insert(0, endpoint)
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
                break
        endpoints.remove(endpoint)
        endpoints.append(endpoint)
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def _validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Invalid map service response")
    if payload.get("remark"):
        raise ValueError(f"Incomplete map data: {payload['remark']}")


def _wait_for_retry(seconds, notify, check):
    until = time.monotonic() + seconds
    while True:
        check()
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        notify(f"map service busy, retrying in {math.ceil(remaining)}s")
        time.sleep(min(1, remaining))


def _cache_path(cache_root: Path | None, query: str) -> Path | None:
    if cache_root is None:
        return None
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()
    return cache_root / f"{digest}.json"


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp_path.replace(path)


def _retry_seconds(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(0.0, (parsedate_to_datetime(retry_after) -
                                    datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        if response.status_code == 429:
            return 15.0
    return min(MAX_RETRY_SLEEP_SECONDS, (2 ** attempt) + random.uniform(0.2, 0.9))


def _split_bbox(south: float, west: float, north: float, east: float,
                max_area_km2: float) -> list[tuple[float, float, float, float]]:
    if max_area_km2 <= 0:
        return [(south, west, north, east)]

    total_area = _bbox_area_km2(south, west, north, east)
    if total_area <= max_area_km2:
        return [(south, west, north, east)]

    width_km, height_km = _bbox_dimensions_km(south, west, north, east)
    target_chunks = max(1, math.ceil(total_area / max_area_km2))
    aspect = max(0.1, width_km / max(0.001, height_km))
    cols = max(1, math.ceil(math.sqrt(target_chunks * aspect)))
    rows = max(1, math.ceil(target_chunks / cols))

    while total_area / (rows * cols) > max_area_km2:
        if width_km / cols > height_km / rows:
            cols += 1
        else:
            rows += 1

    chunks = []
    for row in range(rows):
        cs = south + (north - south) * row / rows
        cn = south + (north - south) * (row + 1) / rows
        for col in range(cols):
            cw = west + (east - west) * col / cols
            ce = west + (east - west) * (col + 1) / cols
            chunks.append((cs, cw, cn, ce))
    return chunks


def _bbox_dimensions_km(south: float, west: float, north: float,
                        east: float) -> tuple[float, float]:
    lat_mid = (south + north) / 2
    height_km = abs(north - south) * 111.32
    width_km = abs(east - west) * 111.32 * _cos_lat(lat_mid)
    return width_km, height_km


def _bbox_area_km2(south: float, west: float, north: float, east: float) -> float:
    width_km, height_km = _bbox_dimensions_km(south, west, north, east)
    return width_km * height_km


def _cos_lat(lat_deg: float) -> float:
    return math.cos(math.radians(lat_deg))


def _parse(payload: dict) -> list[OSMFeature]:
    elements = payload.get("elements", [])
    out: list[OSMFeature] = []
    for el in elements:
        kind = el.get("type")
        tags = el.get("tags", {}) or {}
        if kind == "way":
            coords = [(p["lat"], p["lon"]) for p in el.get("geometry", [])]
            if coords:
                out.append(OSMFeature(el["id"], "way", tags, coords))
        elif kind == "relation":
            rings: list[list[tuple[float, float]]] = []
            role_geoms: list[tuple[str, list[tuple[float, float]]]] = []
            for m in el.get("members", []):
                geom = m.get("geometry")
                if not geom:
                    continue
                ring = [(p["lat"], p["lon"]) for p in geom]
                role_geoms.append((m.get("role", ""), ring))
                rings.append(ring)
            if rings:
                feat = OSMFeature(el["id"], "relation", tags, rings)
                feat.role_geoms = role_geoms
                out.append(feat)
        elif kind == "node":
            lat, lon = el.get("lat"), el.get("lon")
            if lat is not None and lon is not None:
                out.append(OSMFeature(el["id"], "node", tags, [(lat, lon)]))
    return out


def classify(tags: dict) -> str | None:
    """Map OSM tags → a PZ feature category string. None = ignore."""
    if "building" in tags:
        return "building"
    h = tags.get("highway")
    if h:
        if h in {"motorway", "trunk", "primary", "motorway_link", "trunk_link",
                 "primary_link"}:
            return "road_major"
        if h in {"secondary", "tertiary", "secondary_link", "tertiary_link"}:
            return "road_medium"
        if h in {"residential", "unclassified", "service", "living_street",
                 "pedestrian"}:
            return "road_minor"
        if h in {"track", "path", "footway", "cycleway", "bridleway"}:
            return "dirt_path"
        return "road_minor"
    if tags.get("natural") == "water" or tags.get("waterway") in {
            "river", "riverbank", "canal", "stream"}:
        return "water"
    if tags.get("landuse") in {"reservoir", "basin"}:
        return "water"
    if tags.get("landuse") in {"forest"} or tags.get("natural") == "wood":
        return "forest"
    if tags.get("natural") in {"scrub", "heath"}:
        return "scrub"
    if tags.get("natural") == "tree":
        return "tree_single"
    if tags.get("leisure") == "park":
        return "park"
    if tags.get("leisure") in {"garden", "pitch"}:
        return "grass"
    if tags.get("landuse") in {"grass", "meadow", "recreation_ground"}:
        return "grass"
    if tags.get("landuse") in {"farmland", "farmyard"}:
        return "farmland"
    if tags.get("natural") in {"beach", "sand"}:
        return "sand"
    if tags.get("landuse") in {"brownfield", "construction", "quarry"}:
        return "dirt"
    return None
