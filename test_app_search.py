from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as knoxify_app
from generator import renderer


class FakeSearchResponse:
    status_code = 200

    def json(self) -> list[dict]:
        return [{
            "display_name": "Louisville, Jefferson County, Kentucky, United States",
            "lat": "38.2542",
            "lon": "-85.7594",
            "boundingbox": ["38.1", "38.3", "-85.9", "-85.5"],
            "category": "place",
            "type": "city",
            "importance": 0.8,
        }]

    def raise_for_status(self) -> None:
        return None


class SearchRouteTests(unittest.TestCase):
    def test_search_route_normalizes_and_caches_results(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            with mock.patch.object(knoxify_app, "GEOCODE_CACHE_DIR",
                                   Path(cache_dir)):
                with mock.patch("app.requests.get",
                                return_value=FakeSearchResponse()) as get:
                    response = knoxify_app.app.test_client().get(
                        "/api/search?q=Louisville&limit=1"
                    )

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(len(payload["results"]), 1)
                self.assertEqual(payload["results"][0]["type"], "city")
                self.assertEqual(payload["results"][0]["south"], 38.1)
                self.assertEqual(get.call_count, 1)

                with mock.patch("app.requests.get") as get_again:
                    cached = knoxify_app.app.test_client().get(
                        "/api/search?q=Louisville&limit=1"
                    )

                self.assertEqual(cached.status_code, 200)
                get_again.assert_not_called()

    def test_generate_allows_large_map_requests(self) -> None:
        def fake_render(features, south, west, north, east, meters_per_tile,
                        output_dir, map_name, **kwargs):
            out = Path(output_dir)
            return renderer.RenderResult(
                landscape_path=str(out / f"{map_name}.bmp"),
                vegetation_path=str(out / f"{map_name}_veg.bmp"),
                spawn_map_path=str(out / f"{map_name}_ZombieSpawnMap.bmp"),
                preview_path=str(out / f"{map_name}_preview.png"),
                buildings_geojson_path=str(out / f"{map_name}_buildings.geojson"),
                meta_path=str(out / f"{map_name}_info.json"),
                width=6300,
                height=4200,
                cells_x=21,
                cells_y=14,
            )

        with tempfile.TemporaryDirectory() as output_dir:
            with mock.patch.object(knoxify_app, "OUTPUT_DIR",
                                   Path(output_dir)):
                with mock.patch("app.osm.fetch_features",
                                return_value=[]) as fetch:
                    with mock.patch("app.renderer.render",
                                    side_effect=fake_render):
                        response = knoxify_app.app.test_client().post(
                            "/api/generate",
                            json={
                                "south": -33.798127,
                                "west": 151.039869,
                                "north": -33.760904,
                                "east": 151.106461,
                                "metersPerTile": 1.0,
                                "mapName": "large_test",
                            },
                        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["cellsX"], 21)
        self.assertEqual(payload["cellsY"], 14)
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
