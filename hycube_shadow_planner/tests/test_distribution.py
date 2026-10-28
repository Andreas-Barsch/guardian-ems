"""Portable release contracts; all transport is fake and data synthetic."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from hycube_sim import __version__
from hycube_sim.ha_display import ENTITIES, publish
from hycube_sim.model import Policy
from hycube_sim.sources import InputError, ReadClient, SourceError

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_versions_match_release(self):
        self.assertEqual(__version__, "0.5.1")
        self.assertEqual(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], __version__)
        self.assertEqual(json.loads((ROOT / "config.json").read_text())["version"], __version__)
        self.assertIn("Notstromplaner 0.5.1", (ROOT / "README.md").read_text())

    def test_template_does_not_claim_real_approval(self):
        cfg = json.loads((ROOT / "examples/planner.example.json").read_text())
        Policy.parse(cfg["policy"])
        self.assertFalse(cfg["battery_mapping"]["confirmed"])
        self.assertFalse(cfg["policy"]["power_limits_confirmed"])
        self.assertFalse(cfg["ha_display"]["enabled"])
        self.assertTrue(all(not spec["verified"] for spec in cfg["solar_learning"]["measurements"].values()))
        self.assertTrue(cfg["sources"]["home_assistant"]["base_url"].endswith(".invalid"))

    def test_zip_is_reproducible_and_contains_only_sources(self):
        spec = importlib.util.spec_from_file_location("build_zipapp", ROOT / "tools/build_zipapp.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            a = module.build(Path(folder) / "a.pyz")
            b = module.build(Path(folder) / "b.pyz")
            self.assertEqual(hashlib.sha256(a.read_bytes()).digest(), hashlib.sha256(b.read_bytes()).digest())
            with ZipFile(a) as archive:
                expected = {p.relative_to(ROOT).as_posix() for p in (ROOT / "hycube_sim").glob("*.py")}
                self.assertEqual(set(archive.namelist()), expected | {"__main__.py"})

    def test_reader_roles_cannot_cross_into_other_api_or_controls(self):
        class NoNetwork:
            def open(self, *args, **kwargs):
                self.fail = True
                raise AssertionError("Blocked route reached transport")
        for role, routes in {
            "ha": ["/api/research/status", "/api/services", "/api/states/light.example"],
            "guardian": ["/api/states/sensor.example", "/api/services", "/smartCharging"],
        }.items():
            client = ReadClient("https://example.invalid", role, opener=NoNetwork())
            for route in routes:
                with self.subTest(role=role, route=route), self.assertRaises(SourceError):
                    client.read(route)

    def test_disabled_display_never_requests_credentials_or_transport(self):
        class NoNetwork:
            def open(self, *args, **kwargs):
                raise AssertionError("Disabled display attempted a request")
        with patch.dict(os.environ, {}, clear=True):
            publish({}, {"ha_display": {"enabled": False}}, opener=NoNetwork())

    def test_publisher_ignores_configured_write_targets(self):
        requests = []
        class Response:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Capture:
            def open(self, request, **kwargs):
                requests.append(request)
                return Response()
        cfg = {"ha_display": {"enabled": True, "entity_ids": ["switch.example"]},
               "sources": {"home_assistant": {"base_url": "https://example.invalid", "token_env": "TEST_TOKEN"}}}
        with patch.dict(os.environ, {"TEST_TOKEN": "synthetic-test-only"}, clear=True):
            publish({"issues": ["missing synthetic input"]}, cfg, opener=Capture())
        self.assertEqual([r.full_url for r in requests], ["https://example.invalid/api/states/" + e for e in ENTITIES])
        self.assertTrue(all(r.method == "POST" for r in requests))
        self.assertTrue(all(json.loads(r.data)["state"] == "unavailable" for r in requests))
