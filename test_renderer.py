import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from generator import pz_colors as C, renderer
from generator.osm import OSMFeature


class PixelProjection:
    meters_per_tile = 1

    def to_px(self, lat, lon):
        return lon, lat


class RendererTests(unittest.TestCase):
    def test_vectorized_vegetation_preserves_palette_and_existing_output(self):
        size = (300, 300)
        land = Image.new("RGB", size, C.MEDIUM_GRASS)
        draw = ImageDraw.Draw(land)
        draw.rectangle((0, 100, 299, 150), fill=C.WATER)
        draw.rectangle((100, 0, 110, 299), fill=C.LIGHT_ASPHALT)
        veg = Image.new("RGB", size, C.VEG_NOTHING)
        forest = [(10, 10), (290, 10), (290, 290), (10, 290), (10, 10)]
        scrub = [(0, 0), (40, 0), (40, 299), (0, 299), (0, 0)]
        feats = [OSMFeature(1, "way", {"natural": "wood"}, forest),
                 OSMFeature(2, "way", {"natural": "scrub"}, scrub)]
        expected_land, expected_veg = land.copy(), veg.copy()
        mask, scrub_mask = Image.new("L", size), Image.new("L", size)
        ImageDraw.Draw(mask).polygon([(lo, la) for la, lo in forest], fill=255)
        ImageDraw.Draw(scrub_mask).polygon([(lo, la) for la, lo in scrub], fill=255)
        eroded = mask.filter(ImageFilter.MinFilter(5))
        # Reference semantics from the original per-pixel implementation.
        for y in range(size[1]):
            for x in range(size[0]):
                if scrub_mask.getpixel((x, y)):
                    expected_veg.putpixel((x, y), C.BUSHES_TREES_DARK_GRASS)
                if mask.getpixel((x, y)) and expected_land.getpixel((x, y)) != C.WATER:
                    expected_veg.putpixel((x, y), C.TREES if eroded.getpixel((x, y)) else C.TREES_DARK_GRASS)
                    if expected_land.getpixel((x, y)) in (C.MEDIUM_GRASS, C.LIGHT_GRASS):
                        expected_land.putpixel((x, y), C.DARK_GRASS)
        progress = []
        renderer._paint_vegetation(veg, land, feats, PixelProjection(), progress=progress.append)
        np.testing.assert_array_equal(np.asarray(veg), np.asarray(expected_veg))
        np.testing.assert_array_equal(np.asarray(land), np.asarray(expected_land))
        self.assertEqual(progress[-1], 1)

    def test_preview_is_bounded_and_does_not_modify_export_images(self):
        land = Image.new("RGB", (3200, 1800), C.DARK_GRASS)
        veg = Image.new("RGB", land.size, C.TREES)
        preview = renderer._build_preview(land, veg)
        self.assertEqual(preview.size, (1600, 900))
        self.assertEqual(preview.getpixel((0, 0)), (40, 75, 35))
        self.assertEqual(land.size, (3200, 1800))
        self.assertEqual(land.getpixel((0, 0)), C.DARK_GRASS)


if __name__ == "__main__":
    unittest.main()
