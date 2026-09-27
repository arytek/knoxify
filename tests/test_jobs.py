from __future__ import annotations

import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

import app as knoxify
from generator.jobs import JobManager


SELECTION = {"south": 38.04, "west": -84.50, "north": 38.041,
             "east": -84.499, "metersPerTile": 1, "mapName": "test_export"}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = JobManager()
        for name, value in (("OUTPUT_DIR", Path(self.directory.name)), ("jobs", self.manager)):
            patch = mock.patch.object(knoxify, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.client = knoxify.app.test_client()

    def test_background_progress_completion_and_download(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            kwargs["progress"](.5, "1 of 2 areas ready")
            entered.set()
            release.wait(5)
            kwargs["check_cancel"]()
            return []

        with mock.patch("app.osm.fetch_features", side_effect=fetch):
            response = self.client.post("/api/jobs", json=SELECTION)
            self.assertEqual(response.status_code, 202)
            job_id = response.json["id"]
            job = self.manager.get(job_id)
            try:
                self.assertTrue(entered.wait(2))
                status = self.client.get(f"/api/jobs/{job_id}")
                self.assertEqual(status.json["state"], "running")
                self.assertEqual(status.json["progress"], 27.5)
                self.assertIsNone(status.json["result"])
                self.assertEqual(status.headers["Cache-Control"], "no-store")
                busy = self.client.post("/api/jobs", json=SELECTION)
                self.assertEqual(busy.status_code, 409)
                self.assertEqual(busy.json["jobId"], job_id)
                self.assertEqual(self.client.get("/api/jobs/active").json["id"], job_id)
            finally:
                release.set()
                self.assertTrue(job.done.wait(5))

        status = self.client.get(f"/api/jobs/{job_id}").json
        self.assertEqual(status["state"], "complete", status["error"])
        self.assertEqual(status["progress"], 100)
        result = status["result"]
        self.assertGreater(result["totalSeconds"], 0)
        self.assertEqual(set(result["timings"]), {"fetch", "render", "package", "total"})
        self.assertEqual(self.client.get("/api/jobs/active").json, {})
        folder = Path(self.directory.name) / result["mapName"]
        with zipfile.ZipFile(folder / f'{result["mapName"]}.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), 4)
        with Image.open(folder / f'{result["mapName"]}.bmp') as image:
            self.assertEqual(image.size, (300, 300))
            self.assertEqual(image.mode, "RGB")
        with Image.open(folder / f'{result["mapName"]}_ZombieSpawnMap.bmp') as image:
            self.assertEqual(image.size, (30, 30))
        meta = json.loads((folder / f'{result["mapName"]}_info.json').read_text())
        self.assertEqual(meta["timings_seconds"], result["timings"])
        with self.client.get(result["files"]["zip"]) as download:
            self.assertEqual(download.status_code, 200)

    def test_cancellation_and_retry(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            entered.set()
            release.wait(5)
            kwargs["check_cancel"]()
            return []

        with mock.patch("app.osm.fetch_features", side_effect=fetch):
            job_id = self.client.post("/api/jobs", json=SELECTION).json["id"]
            try:
                self.assertTrue(entered.wait(2))
                cancelled = self.client.post(f"/api/jobs/{job_id}/cancel")
                self.assertEqual(cancelled.json["state"], "cancelling")
            finally:
                release.set()
                self.assertTrue(self.manager.get(job_id).done.wait(5))
        status = self.client.get(f"/api/jobs/{job_id}").json
        self.assertEqual(status["state"], "cancelled")
        self.assertIsNone(status["result"])
        with mock.patch("app.osm.fetch_features", return_value=[]):
            retry = self.client.post("/api/generate", json=SELECTION)
        self.assertEqual(retry.status_code, 200)

    def test_fetch_and_render_errors_become_visible_job_failures(self):
        for target in ("app.osm.fetch_features", "app.renderer.render"):
            with self.subTest(target=target):
                with mock.patch("app.osm.fetch_features", return_value=[]):
                    with mock.patch(target, side_effect=RuntimeError("test failure")):
                        job_id = self.client.post("/api/jobs", json=SELECTION).json["id"]
                        self.assertTrue(self.manager.get(job_id).done.wait(5))
                status = self.client.get(f"/api/jobs/{job_id}").json
                self.assertEqual(status["state"], "failed")
                self.assertIn("test failure", status["error"])
                self.assertLess(status["progress"], 100)

    def test_repeat_name_preserves_previous_files(self):
        with mock.patch("app.osm.fetch_features", return_value=[]):
            first = self.client.post("/api/generate", json=SELECTION).json
            second = self.client.post("/api/generate", json=SELECTION).json
        self.assertNotEqual(first["mapName"], second["mapName"])
        self.assertTrue((Path(self.directory.name) / first["mapName"] / "README.txt").exists())

    def test_generation_uses_covering_offline_pack_without_network(self):
        manifest = {"id": "test-pack", "name": "Test Pack", "path": "unused"}

        def local_fetch(_manifest, *args, stats, **kwargs):
            stats.source = "offline"
            stats.pack_id = "test-pack"
            stats.pack_name = "Test Pack"
            stats.chunks_total = stats.chunks_from_cache = 1
            return []

        with mock.patch("app.pack_manager.find_installed", return_value=manifest):
            with mock.patch("app.pack_manager.fetch_features", side_effect=local_fetch) as local:
                with mock.patch("app.osm.fetch_features") as online:
                    response = self.client.post("/api/generate", json=SELECTION)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["fetch"]["source"], "offline")
        self.assertEqual(response.json["fetch"]["pack_name"], "Test Pack")
        local.assert_called_once()
        online.assert_not_called()

    def test_invalid_requests_and_unknown_jobs(self):
        for fields in ({"north": float("inf")}, {"south": -91}, {"east": 181}, {"mapName": 123}):
            self.assertEqual(self.client.post("/api/jobs", json={**SELECTION, **fields}).status_code, 400)
        self.assertEqual(self.client.post("/api/jobs", json=[1]).status_code, 400)
        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 404)
        self.assertEqual(self.client.post("/api/jobs/missing/cancel").status_code, 404)


if __name__ == "__main__":
    unittest.main()
