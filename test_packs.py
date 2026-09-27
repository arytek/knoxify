from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shapely.geometry import box, mapping

import app as knoxify
from generator import osm, packs
from generator.jobs import Job, JobManager


OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Knoxify tests">
  <node id="1" lat="0.5" lon="0.5"><tag k="natural" v="tree"/></node>
  <node id="2" lat="0.4" lon="0.4"/><node id="3" lat="0.6" lon="0.6"/>
  <way id="10"><nd ref="2"/><nd ref="3"/><tag k="highway" v="residential"/></way>
  <node id="4" lat="0.45" lon="0.45"/><node id="5" lat="0.45" lon="0.55"/>
  <node id="6" lat="0.55" lon="0.55"/><node id="7" lat="0.55" lon="0.45"/>
  <way id="11"><nd ref="4"/><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="4"/><tag k="building" v="yes"/></way>
  <node id="20" lat="0.7" lon="0.7"/><node id="21" lat="0.7" lon="0.8"/>
  <node id="22" lat="0.8" lon="0.8"/><node id="23" lat="0.8" lon="0.7"/>
  <way id="12"><nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="20"/></way>
  <relation id="30"><member type="way" ref="12" role="outer"/><tag k="type" v="multipolygon"/><tag k="natural" v="water"/></relation>
</osm>
"""


def catalog_feature(region_id, bounds, name=None, url=None):
    return {
        "type": "Feature",
        "properties": {
            "id": region_id, "name": name or region_id,
            "urls": {"pbf": url or f"https://download.geofabrik.de/{region_id}-latest.osm.pbf"},
        },
        "geometry": mapping(box(*bounds)),
    }


class PackManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = packs.PackManager(self.directory.name)
        self.manager._catalog = {"type": "FeatureCollection", "features": [
            catalog_feature("continent", (-10, -10, 10, 10)),
            catalog_feature("country/region", (0, 0, 1, 1), "country/region"),
        ]}

    def test_recommends_smallest_region_covering_entire_selection(self):
        region = self.manager.recommend(.2, .2, .8, .8)
        self.assertEqual(region["id"], "country/region")
        self.assertEqual(region["name"], "Region")
        self.assertEqual(self.manager.recommend(.8, .8, 1.2, 1.2)["id"], "continent")

    def test_install_is_atomic_preserves_manifest_and_removes_source(self):
        job = Job()

        def download(_url, destination, active):
            destination.write_bytes(b"source")
            active.update(65, "downloaded", "download")

        def prepare(_source, destination, active):
            destination.write_bytes(b"prepared")
            active.update(96, "prepared", "indexing")

        with mock.patch.object(self.manager, "_download", side_effect=download):
            with mock.patch.object(self.manager, "_prepare", side_effect=prepare):
                result = self.manager.install("country/region", job)

        installed = self.manager.get_installed("country/region")
        self.assertEqual(result["name"], "Region")
        self.assertEqual(installed["source_bytes"], 6)
        folder = self.manager._pack_dir("country/region")
        self.assertEqual((folder / "knoxify.osm.pbf").read_bytes(), b"prepared")
        self.assertFalse((folder / "source.osm.pbf").exists())
        self.assertEqual(self.manager.find_installed(.2, .2, .8, .8)["id"], "country/region")
        self.assertTrue(self.manager.remove("country/region"))
        self.assertIsNone(self.manager.get_installed("country/region"))

    def test_rejects_non_geofabrik_download_url(self):
        self.manager._catalog["features"].append(
            catalog_feature("bad", (20, 20, 21, 21), url="https://example.com/data.pbf"))
        with self.assertRaisesRegex(packs.PackError, "not from Geofabrik"):
            self.manager.install("bad", Job())

    def test_prepare_and_query_real_osm_geometry_offline(self):
        source = Path(self.directory.name) / "source.osm"
        prepared = Path(self.directory.name) / "prepared.osm.pbf"
        source.write_text(OSM_XML, encoding="utf-8")
        job = Job()
        self.manager._prepare(source, prepared, job)
        manifest = {"id": "fixture", "name": "Fixture", "path": str(prepared)}
        stats = osm.FetchStats()
        progress = []
        features = self.manager.fetch_features(
            manifest, .3, .3, .9, .9, stats=stats,
            progress=lambda fraction, message: progress.append((fraction, message)))
        categories = {osm.classify(feature.tags) for feature in features}
        self.assertEqual(categories, {"tree_single", "road_minor", "building", "water"})
        self.assertEqual(stats.source, "offline")
        self.assertEqual(stats.http_requests, 0)
        self.assertEqual(progress[-1][0], 1)

    def test_interrupted_download_resumes_from_existing_bytes(self):
        destination = Path(self.directory.name) / "source.osm.pbf"
        partial = destination.with_suffix(".pbf.part")
        partial.write_bytes(b"first")

        class Response:
            status_code = 206
            headers = {"Content-Length": "6"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _size):
                return iter((b"second",))

        job = Job()
        with mock.patch("generator.packs.requests.get", return_value=Response()) as get:
            self.manager._download(
                "https://download.geofabrik.de/test-latest.osm.pbf", destination, job)
        self.assertEqual(destination.read_bytes(), b"firstsecond")
        self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=5-")
        self.assertEqual(job.snapshot()["details"]["bytesTotal"], 11)


class PackRouteTests(unittest.TestCase):
    def setUp(self):
        self.manager = mock.Mock()
        self.jobs = JobManager()
        for name, value in (("pack_manager", self.manager), ("pack_jobs", self.jobs)):
            patch = mock.patch.object(knoxify, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.client = knoxify.app.test_client()

    def test_status_install_poll_and_remove(self):
        status_payload = {"source": "online", "recommended": {"id": "region"}, "installed": []}
        self.manager.status_for_bbox.return_value = status_payload
        response = self.client.get("/api/packs/status?south=0&west=0&north=1&east=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, status_payload)
        self.manager.list_installed.return_value = [{"id": "existing", "name": "Existing"}]
        self.assertEqual(self.client.get("/api/packs").json["installed"][0]["id"], "existing")

        def install(region_id, job, refresh=False):
            job.update(80, "Preparing", "indexing")
            return {"id": region_id, "name": "Region"}

        self.manager.install.side_effect = install
        started = self.client.post("/api/packs/install", json={"regionId": "region"})
        self.assertEqual(started.status_code, 202)
        job = self.jobs.get(started.json["id"])
        self.assertTrue(job.done.wait(2))
        complete = self.client.get(f"/api/packs/jobs/{job.id}")
        self.assertEqual(complete.json["state"], "complete")
        self.assertEqual(complete.json["result"]["name"], "Region")

        self.manager.remove.return_value = True
        removed = self.client.post("/api/packs/remove", json={"regionId": "region"})
        self.assertEqual(removed.json, {"removed": True})

    def test_pack_route_validation(self):
        self.assertEqual(self.client.get("/api/packs/status?south=nope").status_code, 400)
        self.assertEqual(self.client.post("/api/packs/install", json={}).status_code, 400)
        self.assertEqual(self.client.get("/api/packs/jobs/missing").status_code, 404)
        self.assertEqual(self.client.post("/api/packs/jobs/missing/cancel").status_code, 404)
        self.assertEqual(self.client.post("/api/packs/remove", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
