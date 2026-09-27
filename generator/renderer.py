"""Rasterize OSM features into Project Zomboid bitmaps.

Output contract (per the Mapping Guide):
  <name>.bmp              — landscape (11 palette colors)
  <name>_veg.bmp          — vegetation (must be same size as landscape)
  <name>_ZombieSpawnMap.bmp — grayscale, 1/10th resolution

Dimensions are snapped up to the next multiple of 300 (PZ cell size). The
requested real-world bbox is expanded symmetrically to fit that grid so the
meters-per-tile scale stays consistent.
"""
from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
import pyproj
from PIL import Image, ImageDraw, ImageFilter

from . import pz_colors as C
from .osm import OSMFeature, classify


# --- projection ------------------------------------------------------------

@dataclass
class Projector:
    """Latitude/longitude → pixel coordinate in the output bitmap.

    Uses the UTM zone covering the bbox center so distance in meters maps
    nearly linearly to pixels. The projected bbox is expanded up to the next
    300-tile cell multiple.
    """
    south: float
    west: float
    north: float
    east: float
    meters_per_tile: float
    width: int   # pixels (tile count)
    height: int  # pixels
    min_x_m: float
    min_y_m: float
    _transformer: pyproj.Transformer

    @classmethod
    def build(cls, south: float, west: float, north: float, east: float,
              meters_per_tile: float) -> "Projector":
        lon_c = (west + east) / 2
        lat_c = (south + north) / 2
        utm_zone = int((lon_c + 180) / 6) + 1
        epsg = (32600 if lat_c >= 0 else 32700) + utm_zone
        t = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}",
                                        always_xy=True)
        # Project the four corners so we pick up any distortion at the edges.
        corners = [(west, south), (east, south), (east, north), (west, north)]
        xs, ys = zip(*(t.transform(lo, la) for lo, la in corners))
        raw_w = max(xs) - min(xs)
        raw_h = max(ys) - min(ys)
        tiles_w = max(C.CELL_SIZE, _ceil_to(raw_w / meters_per_tile, C.CELL_SIZE))
        tiles_h = max(C.CELL_SIZE, _ceil_to(raw_h / meters_per_tile, C.CELL_SIZE))
        # Re-center: expand bbox in meters to match the rounded-up tile count.
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        half_w_m = (tiles_w * meters_per_tile) / 2
        half_h_m = (tiles_h * meters_per_tile) / 2
        return cls(south, west, north, east, meters_per_tile,
                   tiles_w, tiles_h,
                   cx - half_w_m, cy - half_h_m,
                   t)

    def to_px(self, lat: float, lon: float) -> tuple[float, float]:
        x_m, y_m = self._transformer.transform(lon, lat)
        px = (x_m - self.min_x_m) / self.meters_per_tile
        # Image Y grows downward; UTM Y grows northward → flip.
        py = self.height - (y_m - self.min_y_m) / self.meters_per_tile
        return px, py

    def cell_grid(self) -> tuple[int, int]:
        return self.width // C.CELL_SIZE, self.height // C.CELL_SIZE


def _ceil_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


# --- feature painting ------------------------------------------------------

# Paint order for landscape: later categories overwrite earlier ones, so this
# is effectively painted bottom-to-top.
LANDSCAPE_ORDER = [
    "farmland",       # light grass
    "grass",          # medium grass
    "park",           # medium grass w/ trees added by vegetation pass
    "sand",
    "dirt",
    "dirt_path",      # thin dirt line
    "road_minor",     # light asphalt
    "road_medium",    # medium asphalt
    "road_major",     # dark asphalt
    "water",          # water overrides almost everything
    "building",       # last: buildings take their footprint as dirt (placeholder)
]

# Road widths in meters. Converted to pixels by dividing by meters_per_tile.
ROAD_WIDTHS_M = {
    "road_major": 12.0,
    "road_medium": 8.0,
    "road_minor": 6.0,
    "dirt_path": 2.5,
}

LANDSCAPE_FILL = {
    "water": C.WATER,
    "sand": C.SAND,
    "dirt": C.DIRT,
    "dirt_path": C.DIRT,
    "grass": C.MEDIUM_GRASS,
    "park": C.MEDIUM_GRASS,
    "farmland": C.LIGHT_GRASS,
    "road_minor": C.LIGHT_ASPHALT,
    "road_medium": C.MEDIUM_ASPHALT,
    "road_major": C.DARK_ASPHALT,
    "building": C.DIRT,  # placeholder; user drops .tbx lots on top in WorldEd
}


def _feature_coords_px(feat: OSMFeature, proj: Projector) -> list[list[tuple[float, float]]]:
    """Project every ring/linestring in this feature to pixel space."""
    if feat.kind == "way":
        return [[proj.to_px(la, lo) for la, lo in feat.geometry]]
    if feat.kind == "relation":
        rings: list[list[tuple[float, float]]] = []
        for _role, coords in feat.role_geoms:
            rings.append([proj.to_px(la, lo) for la, lo in coords])
        return rings
    if feat.kind == "node":
        la, lo = feat.geometry[0]
        return [[proj.to_px(la, lo)]]
    return []


def _is_polygon(feat: OSMFeature) -> bool:
    """Treat as polygon if first/last point match (way) or it's a relation."""
    if feat.kind == "relation":
        return True
    if feat.kind == "way" and len(feat.geometry) >= 3:
        return feat.geometry[0] == feat.geometry[-1]
    return False


def _draw_polygon(draw: ImageDraw.ImageDraw, rings: list[list[tuple[float, float]]],
                  fill: tuple[int, int, int]) -> None:
    for ring in rings:
        if len(ring) >= 3:
            draw.polygon(ring, fill=fill)


def _draw_line(draw: ImageDraw.ImageDraw, rings: list[list[tuple[float, float]]],
               fill: tuple[int, int, int], width_px: int) -> None:
    w = max(1, int(round(width_px)))
    for ring in rings:
        if len(ring) >= 2:
            draw.line(ring, fill=fill, width=w, joint="curve")
            # Round line caps so intersections look right.
            r = w // 2
            if r > 0:
                for x, y in ring:
                    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


# --- main entry point ------------------------------------------------------

@dataclass
class RenderResult:
    landscape_path: str
    vegetation_path: str
    spawn_map_path: str
    preview_path: str
    buildings_geojson_path: str
    meta_path: str
    width: int
    height: int
    cells_x: int
    cells_y: int


def render(features: Iterable[OSMFeature], south: float, west: float,
           north: float, east: float, meters_per_tile: float,
           output_dir: str, map_name: str,
           spawn_density: int = 96,
           *,
           extra_meta: dict | None = None,
           progress: Callable[[float, str], None] | None = None,
           check_cancel: Callable[[], None] | None = None) -> RenderResult:
    report = progress or (lambda fraction, message: None)
    check = check_cancel or (lambda: None)
    check()
    report(0, "Preparing terrain")
    proj = Projector.build(south, west, north, east, meters_per_tile)
    landscape = Image.new("RGB", (proj.width, proj.height), C.DARK_GRASS)
    vegetation = Image.new("RGB", (proj.width, proj.height), C.VEG_NOTHING)
    l_draw = ImageDraw.Draw(landscape)

    # Bucket features so we paint in a deterministic order.
    buckets: dict[str, list[OSMFeature]] = {}
    vegetation_feats: list[OSMFeature] = []
    building_feats: list[OSMFeature] = []
    for feat in features:
        cat = classify(feat.tags)
        if cat is None:
            continue
        if cat in {"forest", "scrub", "tree_single"}:
            vegetation_feats.append(feat)
            continue
        if cat == "building":
            building_feats.append(feat)
        buckets.setdefault(cat, []).append(feat)

    total_features = max(1, sum(len(items) for items in buckets.values()))
    painted = 0
    for cat in LANDSCAPE_ORDER:
        fill = LANDSCAPE_FILL.get(cat)
        if fill is None:
            continue
        for feat in buckets.get(cat, []):
            check()
            rings = _feature_coords_px(feat, proj)
            if cat in ROAD_WIDTHS_M:
                width_px = ROAD_WIDTHS_M[cat] / meters_per_tile
                _draw_line(l_draw, rings, fill, int(width_px))
            elif _is_polygon(feat):
                _draw_polygon(l_draw, rings, fill)
            else:
                # Unexpected: linear water like a stream. Draw it narrow.
                _draw_line(l_draw, rings, fill, max(1, int(3 / meters_per_tile)))
            painted += 1
            if painted % 100 == 0:
                report(0.3 * painted / total_features, "Painting roads, water and terrain")

    report(0.3, "Painting vegetation")
    _paint_vegetation(vegetation, landscape, vegetation_feats, proj,
                      progress=lambda fraction: report(0.3 + 0.25 * fraction, "Painting vegetation"),
                      check_cancel=check)

    # --- zombie spawn map (10x smaller, grayscale) ---
    spawn_w = proj.width // C.SPAWN_MAP_SCALE
    spawn_h = proj.height // C.SPAWN_MAP_SCALE
    check()
    report(0.55, "Building zombie population map")
    spawn_map = _build_spawn_map(landscape, spawn_w, spawn_h, spawn_density)

    # --- preview (landscape + vegetation blended) ---
    check()
    report(0.65, "Creating preview")
    preview = _build_preview(landscape, vegetation)

    # --- output files ---
    os.makedirs(output_dir, exist_ok=True)
    landscape_path = os.path.join(output_dir, f"{map_name}.bmp")
    veg_path = os.path.join(output_dir, f"{map_name}_veg.bmp")
    spawn_path = os.path.join(output_dir, f"{map_name}_ZombieSpawnMap.bmp")
    preview_path = os.path.join(output_dir, f"{map_name}_preview.png")
    buildings_path = os.path.join(output_dir, f"{map_name}_buildings.geojson")
    meta_path = os.path.join(output_dir, f"{map_name}_info.json")

    for index, (image, path, kind) in enumerate([
        (landscape, landscape_path, "BMP"), (vegetation, veg_path, "BMP"),
        (spawn_map, spawn_path, "BMP"), (preview, preview_path, "PNG"),
    ]):
        check()
        report(0.7 + index * 0.06, f"Writing image {index + 1} of 4")
        image.save(path, format=kind)
        image.close()

    with open(buildings_path, "w") as f:
        json.dump(_buildings_geojson(building_feats), f)

    cells_x, cells_y = proj.cell_grid()
    meta = {
        "map_name": map_name,
        "bbox": {"south": south, "west": west, "north": north, "east": east},
        "meters_per_tile": meters_per_tile,
        "width_tiles": proj.width,
        "height_tiles": proj.height,
        "cells_x": cells_x,
        "cells_y": cells_y,
        "spawn_density_max": spawn_density,
        "building_count": len(building_feats),
        "guide_reference": "Thuztor Mapping Guide v0.2",
    }
    if extra_meta:
        meta["source"] = extra_meta
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    check()
    report(1, "Map images ready")
    return RenderResult(
        landscape_path=landscape_path,
        vegetation_path=veg_path,
        spawn_map_path=spawn_path,
        preview_path=preview_path,
        buildings_geojson_path=buildings_path,
        meta_path=meta_path,
        width=proj.width,
        height=proj.height,
        cells_x=cells_x,
        cells_y=cells_y,
    )


def _paint_vegetation(veg: Image.Image, landscape: Image.Image,
                      feats: list[OSMFeature], proj: Projector,
                      progress=lambda fraction: None,
                      check_cancel=lambda: None) -> None:
    """Paint trees on the vegetation bitmap.

    Forest polygons get full density (TREES), scrub becomes bushes+trees,
    single-tree nodes become small dots. Forest edges get downgraded to a
    mix with dark grass so the transition isn't a hard rectangle.
    """
    if not feats:
        progress(1)
        return
    mask = Image.new("L", veg.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    scrub_mask = Image.new("L", veg.size, 0)
    scrub_draw = ImageDraw.Draw(scrub_mask)

    for feat in feats:
        check_cancel()
        cat = classify(feat.tags)
        rings = _feature_coords_px(feat, proj)
        if cat == "forest":
            if _is_polygon(feat):
                for ring in rings:
                    if len(ring) >= 3:
                        mask_draw.polygon(ring, fill=255)
        elif cat == "scrub":
            if _is_polygon(feat):
                for ring in rings:
                    if len(ring) >= 3:
                        scrub_draw.polygon(ring, fill=255)
        elif cat == "tree_single":
            if rings and rings[0]:
                x, y = rings[0][0]
                r = max(1, int(2 / proj.meters_per_tile))
                mask_draw.ellipse((x - r, y - r, x + r, y + r), fill=255)

    # Build a "border band" of the forest mask so we can paint edges lighter.
    eroded = mask.filter(ImageFilter.MinFilter(5))
    w, h = veg.size
    # Work in strips to avoid several full-map NumPy copies on large exports.
    rows = max(1, min(256, 1_000_000 // w))
    for y in range(0, h, rows):
        check_cancel()
        box = (0, y, w, min(h, y + rows))
        land = np.array(landscape.crop(box))
        pixels = np.array(veg.crop(box))
        forest = np.asarray(mask.crop(box)) != 0
        dense = np.asarray(eroded.crop(box)) != 0
        scrub = np.asarray(scrub_mask.crop(box)) != 0
        pixels[scrub] = C.BUSHES_TREES_DARK_GRASS
        forest &= ~np.all(land == C.WATER, axis=2)
        pixels[forest & dense] = C.TREES
        pixels[forest & ~dense] = C.TREES_DARK_GRASS
        grass = np.all(land == C.MEDIUM_GRASS, axis=2) | np.all(land == C.LIGHT_GRASS, axis=2)
        land[forest & grass] = C.DARK_GRASS
        veg.paste(Image.fromarray(pixels), box)
        landscape.paste(Image.fromarray(land), box)
        progress(box[3] / h)
    mask.close()
    eroded.close()
    scrub_mask.close()


def _build_spawn_map(landscape: Image.Image, w: int, h: int,
                     max_density: int) -> Image.Image:
    """Grayscale spawn map. Dense in built-up/asphalt areas, zero over water."""
    scaled = landscape.resize((w, h), Image.Resampling.BILINEAR)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    sp = scaled.load()
    op = out.load()
    rng = random.Random(0xABBA)
    for y in range(h):
        for x in range(w):
            r, g, b = sp[x, y]
            # Roughly: asphalt → high, dirt → medium, grass → low, water → 0.
            if r == C.WATER[0] and g == C.WATER[1] and b == C.WATER[2]:
                v = 0
            elif 95 <= r <= 170 and abs(r - g) < 30 and abs(g - b) < 30:
                # grayish = asphalt-ish
                v = max_density
            elif r > g and r > 90 and g < 110:
                # brownish = dirt
                v = int(max_density * 0.55)
            elif g > r and g > 80:
                # greenish = grass
                v = int(max_density * 0.25)
            else:
                v = int(max_density * 0.35)
            # Jitter so it's not blocky — PZ spawns want variety.
            if v:
                v = max(0, min(255, v + rng.randint(-20, 20)))
            op[x, y] = (v, v, v)
    return out


def _build_preview(landscape: Image.Image, vegetation: Image.Image) -> Image.Image:
    """Bounded preview; the export BMPs retain their full resolution."""
    scale = min(1, 1600 / max(landscape.size))
    size = tuple(max(1, round(side * scale)) for side in landscape.size)
    pixels = np.array(landscape.resize(size, Image.Resampling.NEAREST))
    veg = np.asarray(vegetation.resize(size, Image.Resampling.NEAREST))
    for color in (C.TREES, C.TREES_DARK_GRASS, C.SPARSE_TREES, C.BUSHES_TREES_DARK_GRASS):
        pixels[np.all(veg == color, axis=2)] = (40, 75, 35) if color == C.TREES else (65, 95, 45)
    return Image.fromarray(pixels)


def _buildings_geojson(feats: list[OSMFeature]) -> dict:
    """Export building footprints so the user knows where to drop .tbx lots."""
    features = []
    for f in feats:
        if f.kind == "way":
            coords = [[lon, lat] for lat, lon in f.geometry]
            if len(coords) >= 3:
                features.append({
                    "type": "Feature",
                    "properties": {k: v for k, v in f.tags.items() if k in {
                        "building", "name", "addr:housenumber", "addr:street"}},
                    "geometry": {"type": "Polygon", "coordinates": [coords]},
                })
        elif f.kind == "relation":
            polys = []
            for _role, ring in f.role_geoms:
                coords = [[lon, lat] for lat, lon in ring]
                if len(coords) >= 3:
                    polys.append([coords])
            if polys:
                features.append({
                    "type": "Feature",
                    "properties": {k: v for k, v in f.tags.items() if k in {
                        "building", "name"}},
                    "geometry": {"type": "MultiPolygon", "coordinates": polys},
                })
    return {"type": "FeatureCollection", "features": features}
