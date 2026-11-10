"""Extractor policy/topology tests; run with python -m unittest discover -s tests."""

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.extract_network import (  # noqa: E402
    RoadWay, build_graph, car_access_allowed, driving_directions, extract_network,
    is_drivable_way, parse_restriction, write_graph,
)


def identity(xs, ys):
    return xs, ys


def road(way_id, ids, points, **kwargs):
    return RoadWay(str(way_id), tuple(str(i) for i in ids), tuple(points), **kwargs)


class AccessTests(unittest.TestCase):
    def test_ordinary_roads_and_links(self):
        for highway in ("service", "residential", "living_street", "unclassified", "tertiary",
                        "motorway", "trunk", "primary", "secondary", "road", "track",
                        "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"):
            with self.subTest(highway=highway):
                self.assertTrue(is_drivable_way({"highway": highway}))

    def test_nonroads_and_areas_excluded(self):
        for highway in ("footway", "pedestrian", "steps", "cycleway", "path", "bridleway",
                        "construction", "proposed", "busway", "raceway"):
            with self.subTest(highway=highway):
                self.assertFalse(is_drivable_way({"highway": highway, "motorcar": "yes"}))
        self.assertFalse(is_drivable_way({"highway": "service", "area": "yes"}))

    def test_access_specificity(self):
        cases = [
            ({"access": "private"}, False), ({"access": "no"}, False),
            ({"access": "private", "motor_vehicle": "yes"}, True),
            ({"access": "no", "motorcar": "yes"}, True),
            ({"motor_vehicle": "no", "motorcar": "yes"}, True),
            ({"motor_vehicle": "yes", "motorcar": "no"}, False),
            ({"vehicle": "no"}, False), ({"access": "permissive"}, True),
            ({"access": "destination"}, False), ({"access": "customers"}, False),
            ({"access": "not_a_known_value"}, False),
        ]
        for tags, expected in cases:
            with self.subTest(tags=tags):
                self.assertEqual(car_access_allowed(tags), expected)
                self.assertEqual(is_drivable_way({"highway": "service", **tags}), expected)

    def test_ferries(self):
        self.assertTrue(is_drivable_way({"route": "ferry"}))
        self.assertFalse(is_drivable_way({"route": "ferry", "motorcar": "no"}))
        self.assertFalse(is_drivable_way({"route": "ferry", "motor_vehicle": "no"}))
        self.assertTrue(is_drivable_way({"route": "ferry", "motor_vehicle": "no", "motorcar": "yes"}))
        self.assertFalse(is_drivable_way({"route": "ferry", "access": "private"}))

    def test_oneway_and_implicit_roundabout(self):
        self.assertEqual(driving_directions({"oneway": "yes"}), (True, False))
        self.assertEqual(driving_directions({"oneway": "-1"}), (False, True))
        self.assertEqual(driving_directions({"junction": "roundabout"}), (True, False))
        self.assertEqual(driving_directions({"junction": "roundabout", "oneway": "no"}), (True, True))
        self.assertEqual(driving_directions({"oneway": "yes", "oneway:motor_vehicle": "no"}), (True, True))
        self.assertEqual(driving_directions({"oneway:motor_vehicle": "yes", "oneway:motorcar": "-1"}), (False, True))
        self.assertEqual(driving_directions({"oneway": "reversible"}), (False, False))

    def test_directional_access(self):
        self.assertEqual(driving_directions({"motorcar:forward": "no"}), (False, True))
        self.assertEqual(driving_directions({"access:backward": "no"}), (True, False))
        self.assertEqual(driving_directions({"motorcar": "yes", "access:backward": "no"}), (True, True))
        # A conditional is not evaluated against the system clock.
        self.assertTrue(is_drivable_way({"highway": "residential", "access:conditional": "no @ (Mo-Fr)"}))


class RestrictionTests(unittest.TestCase):
    def parse(self, tags=None, members=None, issues=None):
        if members is None:
            members = [("w", 10, "from"), ("n", 2, "via"), ("w", 20, "to")]
        return parse_restriction(100, {"type": "restriction", "restriction": "no_left_turn", **(tags or {})},
                                 members, issues)

    def test_via_node_schema(self):
        self.assertEqual(self.parse(), {"id": "100", "fromWay": "10", "toWay": "20", "viaNode": "2",
                                        "viaWays": [], "restriction": "no_left_turn"})

    def test_same_way_u_turn_keeps_exact_ids(self):
        relation = self.parse({"restriction": "no_u_turn"},
                              [("w", 10, "from"), ("n", 2, "via"), ("w", 10, "to")])
        self.assertEqual(relation["fromWay"], relation["toWay"])
        self.assertEqual(relation["restriction"], "no_u_turn")

    def test_via_way_order_and_same_way_endpoints(self):
        relation = self.parse(members=[("w", 10, "from"), ("w", 30, "via"),
                                       ("w", 11, "via"), ("w", 10, "to")])
        self.assertIsNone(relation["viaNode"])
        self.assertEqual(relation["viaWays"], ["30", "11"])
        self.assertEqual(relation["fromWay"], relation["toWay"])

    def test_mode_overrides_and_exceptions(self):
        self.assertEqual(self.parse({"restriction:motor_vehicle": "only_right_turn"})["restriction"], "only_right_turn")
        self.assertEqual(self.parse({"restriction:motor_vehicle": "only_right_turn",
                                     "restriction:motorcar": "no_u_turn"})["restriction"], "no_u_turn")
        for value in ("motorcar", "bicycle;motor_vehicle", "vehicle"):
            self.assertIsNone(self.parse({"except": value}))
        self.assertIsNotNone(self.parse({"except": "bicycle;bus"}))

    def test_conditionals_and_multi_members_flagged(self):
        issues = []
        self.assertIsNotNone(self.parse({"restriction:conditional": "no_left_turn @ (Mo-Fr)"}, issues=issues))
        self.assertEqual(issues[0]["reason"], "conditional_restriction_not_evaluated")
        for restriction, members in (
            ("no_entry", [("w", 10, "from"), ("w", 11, "from"), ("n", 2, "via"), ("w", 20, "to")]),
            ("no_exit", [("w", 10, "from"), ("n", 2, "via"), ("w", 20, "to"), ("w", 21, "to")]),
        ):
            issues = []
            self.assertIsNone(self.parse({"restriction": restriction}, members, issues))
            self.assertEqual(issues[0]["reason"], "multiple_or_missing_from_to_members")
            self.assertEqual(len(issues[0]["members"]), 4)
        issues = []
        self.assertIsNone(self.parse(members=[("w", 10, "from"), ("w", 20, "to")], issues=issues))
        self.assertEqual(issues[0]["reason"], "missing_or_mixed_via_members")


class GraphTests(unittest.TestCase):
    def test_shared_nodes_split_with_full_geometry(self):
        first = road(10, [1, 2, 3, 4, 5], [(0, 0), (1, 1), (2, 0), (3, 0), (4, 0)])
        second = road(20, [6, 3, 7], [(2, -1), (2, 0), (2, 1)])
        graph = build_graph([second, first], project=identity)
        self.assertEqual(set(graph["nodes"]), {"1", "3", "5", "6", "7"})
        self.assertEqual(len(graph["edges"]), 8)
        first_edge = graph["edges"][0]
        self.assertEqual(first_edge["coordinates"], [[0.0, 0.0], [1.0, 1.0], [2.0, 0.0]])
        self.assertAlmostEqual(first_edge["lengthM"], 2 * math.sqrt(2))
        for forward, reverse in zip(graph["edges"][::2], graph["edges"][1::2]):
            self.assertEqual((forward["u"], forward["v"]), (reverse["v"], reverse["u"]))
            self.assertEqual(forward["coordinates"], list(reversed(reverse["coordinates"])))

    def test_via_node_splits_way_and_no_u_turn_preserved(self):
        relation = parse_restriction(100, {"type": "restriction", "restriction": "no_u_turn"},
                                     [("w", 10, "from"), ("n", 2, "via"), ("w", 10, "to")])
        graph = build_graph([road(10, [1, 2, 3], [(0, 0), (1, 0), (2, 0)])], [relation], project=identity)
        self.assertEqual(set(graph["nodes"]), {"1", "2", "3"})
        self.assertEqual(graph["restrictions"], [relation])
        self.assertEqual(graph["metadata"]["unsupportedRestrictions"], [])

    def test_grade_separation_and_repeated_nodes(self):
        bridge = road(10, [1, 2, 3], [(0, 0), (1, 0), (2, 0)])
        underpass = road(20, [4, 5, 6], [(1, -1), (1, 0), (1, 1)])
        graph = build_graph([bridge, underpass], project=identity)
        self.assertEqual(set(graph["nodes"]), {"1", "3", "4", "6"})
        self.assertEqual(len(graph["edges"]), 4)
        loop = road(30, [7, 8, 9, 8, 10], [(0, 0), (1, 0), (1, 1), (1, 0), (2, 0)])
        graph = build_graph([loop], project=identity)
        self.assertIn("8", graph["nodes"])
        self.assertEqual(len(graph["edges"]), 6)

    def test_reverse_oneway_ferry(self):
        graph = build_graph([road(10, [1, 2], [(0, 0), (1, 0)], forward=False, ferry=True)], project=identity)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual((graph["edges"][0]["u"], graph["edges"][0]["v"]), ("2", "1"))
        self.assertTrue(graph["edges"][0]["ferry"])

    def test_invalid_bounds_and_projection_flagged(self):
        bad = [road(10, [1, 2], [(0, 0), (math.nan, 0)]),
               road(20, [3, 4], [(0, 0), (181, 0)])]
        graph = build_graph(bad, project=identity)
        self.assertEqual(graph["edges"], [])
        self.assertIsNone(graph["metadata"]["bounds"])
        self.assertEqual(graph["metadata"]["counts"]["invalidCoordinateWays"], 2)
        valid = road(30, [5, 6], [(0, 0), (1, 0)])
        graph = build_graph([valid], project=lambda xs, ys: ([math.inf, 0], ys))
        self.assertEqual(graph["metadata"]["invalidCoordinates"][0]["reason"], "invalid_epsg8059_projection")

    def test_deterministic_order_and_atomic_output(self):
        ways = [road(20, [3, 4], [(0, 1), (1, 1)]), road(2, [1, 2], [(0, 0), (1, 0)])]
        a = build_graph(ways, project=identity)
        b = build_graph(reversed(ways), project=identity)
        self.assertEqual(a, b)
        self.assertEqual([edge["id"] for edge in a["edges"]], list(range(4)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            write_graph(a, path)
            original = path.read_bytes()
            self.assertEqual(json.loads(original), a)
            with self.assertRaises(ValueError):
                write_graph({"value": math.nan}, path)
            self.assertEqual(path.read_bytes(), original)


@unittest.skipUnless(importlib.util.find_spec("osmium") and importlib.util.find_spec("pyproj"),
                     "Streaming fixture needs osmium and pyproj")
class StreamingTests(unittest.TestCase):
    def test_streaming_missing_locations_conditionals_and_provenance(self):
        xml = '''<osm version="0.6" generator="extractor-test">
          <node id="1" lon="138.6" lat="-34.9"/>
          <node id="2" lon="138.601" lat="-34.9"/>
          <node id="3" lon="138.602" lat="-34.9"/>
          <node id="4" lon="138.601" lat="-34.901"/>
          <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
            <tag k="highway" v="residential"/><tag k="access:conditional" v="no @ (Mo-Fr)"/></way>
          <way id="20"><nd ref="4"/><nd ref="2"/>
            <tag k="highway" v="service"/><tag k="oneway" v="-1"/></way>
          <way id="30"><nd ref="3"/><nd ref="999"/><tag k="highway" v="residential"/></way>
          <way id="40"><nd ref="1"/><nd ref="4"/><tag k="highway" v="footway"/></way>
          <way id="50"><nd ref="3"/><nd ref="4"/><tag k="route" v="ferry"/></way>
          <relation id="100"><member type="way" ref="10" role="from"/>
            <member type="node" ref="2" role="via"/><member type="way" ref="20" role="to"/>
            <tag k="type" v="restriction"/><tag k="restriction" v="no_right_turn"/></relation>
        </osm>'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.osm"
            target = Path(directory) / "out" / "network.json"
            source.write_text(xml, encoding="utf-8")
            graph = extract_network(source, target)
            meta = graph["metadata"]
            self.assertEqual(len(graph["edges"]), 7)
            self.assertEqual(set(graph["nodes"]), {"1", "2", "3", "4"})
            self.assertEqual(meta["missingLocations"], [{"wayId": "30", "nodeIds": ["999"]}])
            self.assertEqual(meta["counts"]["missingLocationReferences"], 1)
            self.assertEqual(meta["unsupportedTags"][0]["reason"], "conditional_rules_not_evaluated")
            self.assertEqual(meta["unsupportedTags"][0]["tags"], {"access:conditional": "no @ (Mo-Fr)"})
            self.assertEqual(meta["lengthCrs"], "EPSG:8059")
            self.assertEqual(len(meta["source"]["sha256"]), 64)
            self.assertEqual(meta["counts"]["ferryEdges"], 2)
            self.assertEqual(json.loads(target.read_text()), graph)
            self.assertTrue(all(math.isfinite(e["lengthM"]) and e["lengthM"] > 0 for e in graph["edges"]))
            self.assertTrue(all(isinstance(key, str) for key in graph["nodes"]))


class ToolboxTests(unittest.TestCase):
    def setUp(self):
        self.arcpy = MagicMock()
        self.arcpy.ExecuteError = type("ExecuteError", (Exception,), {})
        loader = SourceFileLoader("support_heatmap_network_test", str(ROOT / "SupportHeatmapNetwork.pyt"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"arcpy": self.arcpy}):
            loader.exec_module(self.module)

    def test_ui_defaults_and_license(self):
        self.arcpy.Parameter.side_effect = lambda **kwargs: SimpleNamespace(filter=SimpleNamespace(list=[]), **kwargs)
        tool = self.module.ExtractRoadGraph()
        params = tool.getParameterInfo()
        self.assertEqual([p.direction for p in params], ["Input", "Output", "Input"])
        self.assertEqual(params[2].value, sys.executable)
        self.assertEqual(params[0].filter.list, ["pbf"])
        self.assertTrue(tool.isLicensed())
        self.arcpy.CheckExtension.assert_not_called()

    def test_external_interpreter_receives_literal_paths(self):
        params = [SimpleNamespace(valueAsText=p) for p in (
            "C:/OSM data/roads.pbf", "C:/Output data/network.json", "C:/Python clone/python.exe")]
        with patch.object(self.module.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="graph ready\n", stderr="")) as run:
            self.module.ExtractRoadGraph().execute(params, None)
        command = run.call_args.args[0]
        self.assertEqual(command, [params[2].valueAsText, str(ROOT / "scripts" / "extract_network.py"),
                                   "--pbf", params[0].valueAsText, "--output", params[1].valueAsText])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.arcpy.SetParameterAsText.assert_called_once_with(1, params[1].valueAsText)
        self.arcpy.ResetProgressor.assert_called_once()

    def test_failed_extraction_reports_error_and_resets_progress(self):
        params = [SimpleNamespace(valueAsText=p) for p in ("input.pbf", "output.json", "python")]
        with patch.object(self.module.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="No module named osmium")):
            with self.assertRaises(self.arcpy.ExecuteError):
                self.module.ExtractRoadGraph().execute(params, None)
        self.arcpy.AddError.assert_called_once_with("No module named osmium")
        self.arcpy.SetParameterAsText.assert_not_called()
        self.arcpy.ResetProgressor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
