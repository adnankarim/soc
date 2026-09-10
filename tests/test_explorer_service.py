"""Focused tests for the agent-facing service contract.

The tests use a temporary design copy.  That proves session and persistent
semantics without ever changing ``data/designs/orion_soc.json``.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from soc_explorer.adapter import ExplorerAdapter
from soc_explorer.service import ExplorerService


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "data" / "designs" / "orion_soc.json"


class ExplorerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.design_copy = Path(self.temp_dir.name) / "orion_soc.json"
        shutil.copy2(DESIGN, self.design_copy)
        self.service = ExplorerService(self.design_copy)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_catalog_covers_all_required_element_operations(self) -> None:
        catalog = self.service.list_tools()
        names = {tool["name"] for tool in catalog}
        self.assertTrue(
            {
                "list_components",
                "list_links",
                "get_component",
                "get_link",
                "search",
                "validate_design",
                "update_property",
                "export_report",
            }.issubset(names)
        )
        update_tool = next(tool for tool in catalog if tool["name"] == "update_property")
        self.assertTrue(update_tool["mutates"])
        self.assertEqual(update_tool["arity"], 3)

    def test_list_and_get_are_sorted_and_json_safe(self) -> None:
        components = self.service.call("list_components", {})
        self.assertTrue(components["ok"])
        self.assertEqual([item["id"] for item in components["result"]], sorted(
            item["id"] for item in components["result"]
        ))
        cpu = self.service.call("get_component", {"component_id": "cpu0"})
        self.assertTrue(cpu["ok"])
        self.assertEqual(cpu["result"]["kind"], "initiator")
        links = self.service.call("list_links", {"src": "r0"})
        self.assertTrue(links["ok"])
        self.assertTrue(all(link["src"] == "r0" for link in links["result"]))

    def test_missing_and_invalid_input_have_stable_errors(self) -> None:
        missing = self.service.call("get_component", {"component_id": "absent"})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["type"], "NotFoundError")
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")

        bad_boolean = self.service.call(
            "update_property",
            {"element_id": "cpu0", "prop_path": "qos", "value": "low", "persist": "false"},
        )
        self.assertFalse(bad_boolean["ok"])
        self.assertEqual(bad_boolean["error"]["code"], "INVALID_ARGUMENT")

        unexpected = self.service.call("list_components", {"sort": "name"})
        self.assertFalse(unexpected["ok"])
        self.assertEqual(unexpected["error"]["code"], "UNEXPECTED_ARGUMENT")

        # The stock MiniEda helper would replace the scalar ``qos`` with a
        # dictionary here. The explorer refuses that surprising destructive path.
        conflict = self.service.call(
            "update_property",
            {"element_id": "cpu0", "prop_path": "qos.level", "value": 3},
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "PROPERTY_PATH_CONFLICT")
        self.assertEqual(
            self.service.call("get_component", {"component_id": "cpu0"})["result"]["properties"]["qos"],
            "high",
        )

    def test_smart_search_avoids_a_substring_false_positive(self) -> None:
        result = self.service.call("search", {"query": "r0"})
        self.assertTrue(result["ok"])
        component_ids = [hit["id"] for hit in result["result"] if hit["type"] == "component"]
        link_ids = [hit["id"] for hit in result["result"] if hit["type"] == "link"]
        self.assertEqual(component_ids, ["r0"])
        self.assertNotIn("ddr0", component_ids)
        self.assertEqual(link_ids, ["L1", "L2", "L3", "L4", "L5", "L8", "L_loop"])

        substring = self.service.call(
            "search", {"query": "r0", "scope": "components", "match_mode": "substring"}
        )
        self.assertTrue(substring["ok"])
        self.assertIn("ddr0", [hit["id"] for hit in substring["result"]])

    def test_graph_validation_finds_the_two_intentional_orion_issues(self) -> None:
        result = self.service.call("validate_design", {})
        self.assertTrue(result["ok"])
        validation = result["result"]
        self.assertFalse(validation["valid"])
        codes = {issue["code"] for issue in validation["issues"]}
        self.assertIn("DANGLING_LINK_DESTINATION", codes)
        self.assertIn("SELF_LOOP", codes)
        dangling = next(
            issue
            for issue in validation["issues"]
            if issue["code"] == "DANGLING_LINK_DESTINATION"
        )
        self.assertEqual(dangling["element_id"], "L7")
        self.assertEqual(dangling["details"]["component_id"], "sram_l2")

    def test_dry_run_changes_nothing_and_session_update_is_visible_only_in_memory(self) -> None:
        original_hash = _sha256(self.design_copy)
        dry_run = self.service.call(
            "update_property",
            {
                "element_id": "cpu0",
                "prop_path": "qos",
                "value": "low",
                "dry_run": True,
            },
        )
        self.assertTrue(dry_run["ok"])
        self.assertFalse(dry_run["result"]["applied"])
        self.assertEqual(self.service.call("get_component", {"component_id": "cpu0"})["result"]["properties"]["qos"], "high")
        self.assertEqual(_sha256(self.design_copy), original_hash)

        session_update = self.service.call(
            "update_property",
            {"element_id": "cpu0", "prop_path": "qos", "value": "medium", "persist": False},
        )
        self.assertTrue(session_update["ok"])
        self.assertEqual(session_update["result"]["mode"], "session")
        self.assertEqual(session_update["result"]["file_sha256_before"], original_hash)
        self.assertEqual(session_update["result"]["file_sha256_after"], original_hash)
        self.assertEqual(self.service.call("get_component", {"component_id": "cpu0"})["result"]["properties"]["qos"], "medium")
        self.assertEqual(_sha256(self.design_copy), original_hash)

        # A fresh service reads the untouched file, not the old process's session.
        fresh_service = ExplorerService(self.design_copy)
        self.assertEqual(fresh_service.call("get_component", {"component_id": "cpu0"})["result"]["properties"]["qos"], "high")

    def test_persistent_write_requires_opt_in_and_then_survives_reload(self) -> None:
        denied = self.service.call(
            "update_property",
            {"element_id": "cpu0", "prop_path": "qos", "value": "low", "persist": True},
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "PERSISTENCE_DISABLED")

        writable = ExplorerService(self.design_copy, allow_persist=True)
        applied = writable.call(
            "update_property",
            {"element_id": "cpu0", "prop_path": "qos", "value": "low", "persist": True},
        )
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["result"]["persisted"])
        reread = ExplorerService(self.design_copy).call("get_component", {"component_id": "cpu0"})
        self.assertEqual(reread["result"]["properties"]["qos"], "low")

    def test_service_constructor_protects_the_canonical_fixture_too(self) -> None:
        with self.assertRaises(ValueError):
            ExplorerService(DESIGN, allow_persist=True)

    def test_report_is_readable_and_mentions_the_design(self) -> None:
        response = self.service.call("export_report", {"format": "text"})
        self.assertTrue(response["ok"])
        self.assertIn("orion_soc", response["result"])
        self.assertIn("DANGLING_LINK_DESTINATION", response["result"])

    def test_adapter_uses_the_same_service_contract(self) -> None:
        adapter = ExplorerAdapter(self.design_copy)
        response = adapter.call("validate_design", {})
        self.assertTrue(response["ok"])
        self.assertEqual(
            {issue["code"] for issue in response["result"]["issues"]},
            {"DANGLING_LINK_DESTINATION", "SELF_LOOP"},
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
