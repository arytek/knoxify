from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

import requests

from generator import osm
from generator.jobs import Cancelled


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class OSMFetchTests(unittest.TestCase):
    def test_split_bbox_has_no_hard_chunk_cap(self) -> None:
        chunks = osm._split_bbox(0.0, 0.0, 1.0, 1.0, max_area_km2=1.0)

        self.assertGreater(len(chunks), 64)

    def test_fetch_dedupes_chunk_results_and_reuses_cache(self) -> None:
        payload = {
            "elements": [{
                "type": "way",
                "id": 123,
                "tags": {"highway": "residential"},
                "geometry": [
                    {"lat": 0.0, "lon": 0.0},
                    {"lat": 0.01, "lon": 0.01},
                ],
            }],
        }

        with tempfile.TemporaryDirectory() as cache_dir:
            stats = osm.FetchStats()
            with mock.patch("generator.osm.requests.Session.post",
                            return_value=FakeResponse(payload)) as post:
                features = osm.fetch_features(
                    0.0, 0.0, 0.05, 0.05,
                    stats=stats,
                    cache_dir=cache_dir,
                    max_chunk_area_km2=10.0,
                )

            self.assertGreater(stats.chunks_total, 1)
            self.assertEqual(post.call_count, stats.chunks_total)
            self.assertEqual(len(features), 1)
            self.assertEqual(features[0].osm_id, 123)

            cached_stats = osm.FetchStats()
            with mock.patch("generator.osm.requests.Session.post") as post:
                cached_features = osm.fetch_features(
                    0.0, 0.0, 0.05, 0.05,
                    stats=cached_stats,
                    cache_dir=cache_dir,
                    max_chunk_area_km2=10.0,
                )

            post.assert_not_called()
            self.assertEqual(cached_stats.chunks_from_cache,
                             cached_stats.chunks_total)
            self.assertEqual(len(cached_features), 1)

    def test_healthy_endpoint_is_reused_for_next_chunk(self):
        endpoints = ["https://slow.example/api", "https://healthy.example/api"]
        with mock.patch.object(osm, "OVERPASS_ENDPOINTS", endpoints):
            with mock.patch("generator.osm.requests.Session.post", side_effect=[
                requests.Timeout(), FakeResponse({"elements": []}), FakeResponse({"elements": []}),
            ]) as post:
                with mock.patch.object(osm, "_split_bbox", return_value=[(0, 0, .01, .01), (.01, 0, .02, .01)]):
                    stats = osm.FetchStats()
                    osm.fetch_features(0, 0, .02, .01, stats=stats, use_cache=False)
        self.assertEqual([call.args[0] for call in post.call_args_list], [endpoints[0], endpoints[1], endpoints[1]])
        self.assertEqual(stats.retries, 1)

    def test_rate_limit_respects_retry_after_without_rotating_servers(self):
        response = FakeResponse({}, 429)
        response.headers = {"Retry-After": "120"}
        with mock.patch("generator.osm.requests.Session.post", side_effect=[response, FakeResponse({"elements": []})]) as post:
            with mock.patch.object(osm, "_wait_for_retry") as wait:
                osm.fetch_features(0, 0, .001, .001, use_cache=False)
        self.assertEqual(wait.call_args.args[0], 120)
        self.assertEqual(post.call_args_list[0].args[0], post.call_args_list[1].args[0])

    def test_incomplete_payload_is_never_cached_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("generator.osm.requests.Session.post", return_value=FakeResponse({
                "elements": [], "remark": "runtime error: Query timed out",
            })):
                with self.assertRaisesRegex(RuntimeError, "Incomplete map data"):
                    osm.fetch_features(0, 0, .001, .001, cache_dir=directory)
            self.assertFalse(list(Path(directory).glob("*.json")))

    def test_corrupt_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as directory:
            query = osm._build_query(0, 0, .001, .001)
            osm._cache_path(Path(directory), query).write_text("broken", encoding="utf-8")
            with mock.patch("generator.osm.requests.Session.post", return_value=FakeResponse({"elements": []})) as post:
                osm.fetch_features(0, 0, .001, .001, cache_dir=directory)
            self.assertEqual(post.call_count, 1)

    def test_cancel_interrupts_rate_limit_wait(self):
        with mock.patch("generator.osm.time.sleep") as sleep:
            with self.assertRaises(Cancelled):
                osm._wait_for_retry(120, mock.Mock(), mock.Mock(side_effect=Cancelled))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
