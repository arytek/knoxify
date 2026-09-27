"""Download, prepare, discover, and query local Geofabrik OSM packs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import osmium
import requests
from shapely.geometry import box, shape

from . import osm

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
INDEX_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_PACK_DIR = Path(__file__).resolve().parents[1] / "output" / "_packs"
RELEVANT_KEYS = ("natural", "waterway", "landuse", "leisure", "highway", "building")
GEO_TAGS = RELEVANT_KEYS + ("name", "addr:housenumber", "addr:street")
_SAFE_PART = re.compile(r"[^a-z0-9_-]+")


class PackError(RuntimeError):
    pass


class PackManager:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root) if root is not None else DEFAULT_PACK_DIR
        self._catalog_lock = threading.Lock()
        self._catalog: dict | None = None

    def status_for_bbox(self, south: float, west: float,
                        north: float, east: float) -> dict:
        installed = self.find_installed(south, west, north, east)
        result = {
            "source": "offline" if installed else "online",
            "installedCovering": _public_manifest(installed) if installed else None,
            "installed": self.list_installed(),
            "recommended": None,
        }
        try:
            region = self.recommend(south, west, north, east)
        except (OSError, ValueError, requests.RequestException) as exc:
            result["catalogError"] = str(exc)
            return result
        if region:
            manifest = self.get_installed(region["id"])
            result["recommended"] = {
                "id": region["id"],
                "name": region["name"],
                "installed": manifest is not None,
                "downloadBytes": None,
                "updatedAt": manifest.get("installed_at") if manifest else None,
                "storedBytes": manifest.get("stored_bytes") if manifest else None,
            }
        return result

    def recommend(self, south: float, west: float,
                  north: float, east: float) -> dict | None:
        selection = box(west, south, east, north)
        matches = []
        for feature in self._catalog_features():
            props = feature.get("properties", {})
            url = props.get("urls", {}).get("pbf")
            geometry = feature.get("geometry")
            if not url or not geometry:
                continue
            try:
                boundary = shape(geometry)
            except (TypeError, ValueError):
                continue
            if boundary.covers(selection):
                matches.append((boundary.area, props, geometry))
        if not matches:
            return None
        _, props, geometry = min(matches, key=lambda item: item[0])
        return {
            "id": props["id"],
            "name": _display_name(props),
            "url": props["urls"]["pbf"],
            "geometry": geometry,
        }

    def install(self, region_id: str, job, *, refresh: bool = False) -> dict:
        region = self._region_by_id(region_id)
        if region is None:
            raise PackError("Unknown regional data pack.")
        _validate_download_url(region["url"])

        pack_dir = self._pack_dir(region_id)
        pack_dir.mkdir(parents=True, exist_ok=True)
        source_path = pack_dir / "source.osm.pbf"
        prepared_path = pack_dir / "knoxify.osm.pbf"

        # A complete source left by an interrupted preparation is reusable.
        # Normal successful installs remove it, so Update still downloads fresh data.
        if not source_path.exists():
            self._download(region["url"], source_path, job)
        else:
            job.update(65, "Using the completed regional download", "download")

        source_bytes = source_path.stat().st_size
        part_path = pack_dir / "knoxify.part.osm.pbf"
        try:
            self._prepare(source_path, part_path, job)
            job.check()
            part_path.replace(prepared_path)
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise

        manifest = {
            "id": region["id"],
            "name": region["name"],
            "source_url": region["url"],
            "geometry": region["geometry"],
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source_bytes": source_bytes,
            "stored_bytes": prepared_path.stat().st_size,
            "format": 1,
        }
        _write_json_atomic(pack_dir / "manifest.json", manifest)
        # The prepared file contains all references Knoxify needs.
        source_path.unlink(missing_ok=True)
        (pack_dir / "source.osm.pbf.part").unlink(missing_ok=True)
        job.update(99, f"{region['name']} is ready for offline generation", "complete")
        return _public_manifest(manifest)

    def remove(self, region_id: str) -> bool:
        path = self._pack_dir(region_id).resolve()
        root = self.root.resolve()
        if path.parent != root:
            raise PackError("Invalid pack location.")
        if not path.exists():
            return False
        shutil.rmtree(path)
        return True

    def list_installed(self) -> list[dict]:
        if not self.root.exists():
            return []
        packs = []
        for path in self.root.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if (path.parent / "knoxify.osm.pbf").is_file():
                    packs.append(_public_manifest(manifest))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(packs, key=lambda item: item["name"])

    def get_installed(self, region_id: str) -> dict | None:
        path = self._pack_dir(region_id) / "manifest.json"
        prepared = path.parent / "knoxify.osm.pbf"
        if not path.is_file() or not prepared.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def find_installed(self, south: float, west: float,
                       north: float, east: float) -> dict | None:
        selection = box(west, south, east, north)
        matches = []
        for public in self.list_installed():
            manifest = self.get_installed(public["id"])
            if not manifest:
                continue
            try:
                boundary = shape(manifest["geometry"])
            except (KeyError, TypeError, ValueError):
                continue
            if boundary.covers(selection):
                manifest["path"] = str(self._pack_dir(public["id"]) / "knoxify.osm.pbf")
                matches.append((boundary.area, manifest))
        return min(matches, key=lambda item: item[0])[1] if matches else None

    def fetch_features(self, manifest: dict, south: float, west: float,
                       north: float, east: float, *, stats: osm.FetchStats,
                       progress=None, check_cancel=None) -> list[osm.OSMFeature]:
        started = time.monotonic()
        report = progress or (lambda fraction, message: None)
        check = check_cancel or (lambda: None)
        stats.source = "offline"
        stats.pack_id = manifest["id"]
        stats.pack_name = manifest["name"]
        stats.chunks_total = 1
        stats.chunks_from_cache = 1
        stats.cache_dir = str(self._pack_dir(manifest["id"]))
        report(0.05, f"Reading local {manifest['name']} data")
        selection = box(west, south, east, north)

        processor = (osmium.FileProcessor(manifest["path"])
                     .with_locations("sparse_mem_array")
                     .with_areas()
                     .with_filter(osmium.filter.KeyFilter(*RELEVANT_KEYS))
                     .with_filter(osmium.filter.GeoInterfaceFilter(tags=GEO_TAGS)))
        seen: dict[tuple[str, int], osm.OSMFeature] = {}
        processed = 0
        for obj in processor:
            processed += 1
            if processed % 1000 == 0:
                check()
            tags = dict(obj.tags)
            category = osm.classify(tags)
            if category is None:
                continue
            geometry = obj.__geo_interface__["geometry"]
            if not _geometry_intersects_bbox(geometry, selection):
                continue
            feature = _object_to_feature(obj, tags, geometry)
            if feature is not None:
                seen.setdefault((feature.kind, feature.osm_id), feature)

        check()
        stats.seconds = time.monotonic() - started
        report(1, f"Local {manifest['name']} data ready")
        return list(seen.values())

    def _catalog_features(self) -> list[dict]:
        with self._catalog_lock:
            if self._catalog is None:
                self._catalog = self._load_catalog()
            return self._catalog.get("features", [])

    def _load_catalog(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        cache_path = self.root / "geofabrik-index.json"
        cached = None
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                age = time.time() - cache_path.stat().st_mtime
                if age < INDEX_MAX_AGE_SECONDS:
                    return cached
            except (OSError, ValueError):
                cached = None
        try:
            response = requests.get(INDEX_URL, headers={"User-Agent": osm.USER_AGENT}, timeout=(10, 30))
            response.raise_for_status()
            catalog = response.json()
            if catalog.get("type") != "FeatureCollection":
                raise ValueError("Invalid regional data index.")
            _write_json_atomic(cache_path, catalog)
            return catalog
        except (requests.RequestException, ValueError):
            if cached is not None:
                return cached
            raise

    def _region_by_id(self, region_id: str) -> dict | None:
        for feature in self._catalog_features():
            props = feature.get("properties", {})
            if props.get("id") == region_id and props.get("urls", {}).get("pbf"):
                return {
                    "id": region_id,
                    "name": _display_name(props),
                    "url": props["urls"]["pbf"],
                    "geometry": feature.get("geometry"),
                }
        return None

    def _pack_dir(self, region_id: str) -> Path:
        slug = _SAFE_PART.sub("-", region_id.lower()).strip("-")[:60] or "region"
        digest = hashlib.sha1(region_id.encode("utf-8")).hexdigest()[:8]
        return self.root / f"{slug}-{digest}"

    @staticmethod
    def _download(url: str, destination: Path, job) -> None:
        partial = destination.with_suffix(destination.suffix + ".part")
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": osm.USER_AGENT}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        with requests.get(url, headers=headers, stream=True,
                          timeout=(15, 90)) as response:
            response.raise_for_status()
            resumed = existing > 0 and response.status_code == 206
            mode = "ab" if resumed else "wb"
            downloaded = existing if resumed else 0
            content_length = int(response.headers.get("Content-Length", 0))
            total = downloaded + content_length if content_length else 0
            if total and shutil.disk_usage(destination.parent).free < total * 2:
                raise PackError("Not enough free disk space to download and prepare this region.")
            with partial.open(mode) as output:
                for block in response.iter_content(1024 * 1024):
                    job.check()
                    if not block:
                        continue
                    output.write(block)
                    downloaded += len(block)
                    fraction = downloaded / total if total else 0
                    size_text = _format_bytes(downloaded)
                    if total:
                        size_text += f" of {_format_bytes(total)}"
                    job.update(max(1, fraction * 65), f"Downloading regional data: {size_text}",
                               "download", bytesDownloaded=downloaded, bytesTotal=total)
        if total and downloaded != total:
            raise PackError("Regional download ended before all data arrived. Retry to resume it.")
        partial.replace(destination)

    @staticmethod
    def _prepare(source: Path, destination: Path, job) -> None:
        job.update(68, "Preparing regional data for Knoxify", "indexing")
        processor = (osmium.FileProcessor(source)
                     .with_filter(osmium.filter.KeyFilter(*RELEVANT_KEYS)))
        writer = osmium.BackReferenceWriter(destination, ref_src=source, overwrite=True)
        selected = 0
        processed = 0
        try:
            for obj in processor:
                processed += 1
                if processed % 2000 == 0:
                    job.check()
                category = osm.classify(dict(obj.tags))
                if category is None or (obj.is_node() and category != "tree_single"):
                    continue
                writer.add(obj)
                selected += 1
                if selected % 5000 == 0:
                    job.update(75, f"Preparing regional data: {selected:,} map features", "indexing")
            job.check()
            job.update(88, "Adding geometry references", "indexing")
            writer.close()
        except BaseException:
            # BackReferenceWriter owns temporary files; close best-effort before cleanup.
            try:
                writer.close()
            except Exception:
                pass
            raise
        if selected == 0 or not destination.is_file():
            raise PackError("The regional download did not contain usable map features.")
        job.update(96, f"Prepared {selected:,} map features", "indexing")


def _object_to_feature(obj, tags: dict, geometry: dict) -> osm.OSMFeature | None:
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if obj.is_node() and kind == "Point":
        lon, lat = coords
        return osm.OSMFeature(int(obj.id), "node", tags, [(lat, lon)])
    if obj.is_way() and kind == "LineString":
        points = [(lat, lon) for lon, lat in coords]
        return osm.OSMFeature(int(obj.id), "way", tags, points) if points else None
    if obj.is_area():
        if obj.from_way():
            return None  # The original closed way is already present.
        polygons = coords if kind == "MultiPolygon" else [coords]
        role_geoms = []
        rings = []
        for polygon in polygons:
            for index, ring in enumerate(polygon):
                converted = [(lat, lon) for lon, lat in ring]
                if converted:
                    role_geoms.append(("outer" if index == 0 else "inner", converted))
                    rings.append(converted)
        if rings:
            feature = osm.OSMFeature(int(obj.orig_id()), "relation", tags, rings)
            feature.role_geoms = role_geoms
            return feature
    return None


def _geometry_intersects_bbox(geometry: dict, selection) -> bool:
    bounds = _coordinate_bounds(geometry.get("coordinates"))
    if not bounds or not box(*bounds).intersects(selection):
        return False
    try:
        return shape(geometry).intersects(selection)
    except (TypeError, ValueError):
        return False


def _coordinate_bounds(coordinates) -> tuple[float, float, float, float] | None:
    if not coordinates:
        return None
    if isinstance(coordinates[0], (int, float)):
        lon, lat = coordinates[:2]
        return float(lon), float(lat), float(lon), float(lat)
    bounds = [item for item in (_coordinate_bounds(value) for value in coordinates) if item]
    if not bounds:
        return None
    return (min(item[0] for item in bounds), min(item[1] for item in bounds),
            max(item[2] for item in bounds), max(item[3] for item in bounds))


def _display_name(properties: dict) -> str:
    raw = properties.get("name") or properties.get("id") or "Regional data"
    return raw.rsplit("/", 1)[-1].replace("-", " ").title()


def _public_manifest(manifest: dict | None) -> dict | None:
    if not manifest:
        return None
    return {key: manifest.get(key) for key in
            ("id", "name", "installed_at", "source_bytes", "stored_bytes")}


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "download.geofabrik.de":
        raise PackError("Regional download URL is not from Geofabrik.")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
