"""Query OpenStreetMap via the Overpass API.

We only pull tags that map cleanly onto PZ terrain categories — everything
else is ignored. The query asks for a single bbox and returns ways/relations
with their full geometry so we can rasterize without a second roundtrip.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import requests

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]

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
                   timeout: int = 60) -> list[OSMFeature]:
    """Run the Overpass query against the first endpoint that answers."""
    query = _build_query(south, west, north, east, timeout=timeout)
    last_err: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": query}, timeout=timeout + 10)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = RuntimeError(f"{endpoint} -> {r.status_code}")
                time.sleep(2)
                continue
            r.raise_for_status()
            return _parse(r.json())
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            continue
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


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
