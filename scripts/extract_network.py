#!/usr/bin/env python3
"""Extract a directed, distance-only motorcar graph from a local OSM file.

Only osmium and pyproj are needed for extraction. Utility functions stay usable
without either dependency. Nodes are junction/end/via-node OSM IDs, not every
shape point; edge coordinates retain every intervening OSM point. Crossings
connect only when they share an OSM node ID. Edge lengths use EPSG:8059 metres.

Conditional rules are recorded, not evaluated: this is a static base-tag graph.
Limited access (destination/customers/delivery/permit/etc.) is excluded because
a static public through-road graph cannot enforce those journeys' purposes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Callable, Iterable, Mapping, Sequence


if sys.version_info < (3, 10):
    raise SystemExit("The road extractor requires Python 3.10 or newer; select a compatible Python executable.")


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PBF = ROOT / "south-australia-261108.osm.pbf"
DEFAULT_OUTPUT = ROOT / ".generated" / "network.json"
ROAD_TYPES = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "road", "track",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
})
ACCESS_MODES = ("motorcar", "motor_vehicle", "vehicle", "access")
ALLOW_ACCESS = frozenset({"yes", "permissive", "designated", "official"})
DENY_ACCESS = frozenset({
    "no", "private", "destination", "customers", "delivery", "agricultural",
    "forestry", "permit", "permit_holder", "emergency", "discouraged",
})
YES = frozenset({"yes", "1", "true"})
NO = frozenset({"no", "0", "false"})
RESTRICTION_TYPES = frozenset({
    "no_left_turn", "no_right_turn", "no_straight_on", "no_u_turn", "no_entry",
    "no_exit", "only_left_turn", "only_right_turn", "only_straight_on", "only_u_turn",
})
Coordinate = tuple[float, float]
Projector = Callable[[Sequence[float], Sequence[float]], tuple[Sequence[float], Sequence[float]]]


@dataclass(frozen=True, slots=True)
class RoadWay:
    """An accepted OSM way, in original OSM node order (IDs are strings)."""

    id: str
    node_ids: tuple[str, ...]
    coordinates: tuple[Coordinate, ...]
    forward: bool = True
    backward: bool = True
    ferry: bool = False


def _tag(tags: Mapping[str, str], key: str) -> str:
    return str(tags.get(key, "")).strip().lower()


def _access(tags: Mapping[str, str], direction: str | None = None) -> tuple[str, str]:
    # A mode-specific base permission overrides a less-specific directional tag.
    for mode in ACCESS_MODES:
        keys = (f"{mode}:{direction}", mode) if direction else (mode,)
        for key in keys:
            value = _tag(tags, key)
            if value:
                return key, value
    return "", "yes"


def car_access_allowed(tags: Mapping[str, str], direction: str | None = None) -> bool:
    """Evaluate access hierarchy; unknown and purpose-limited access is excluded."""
    return _access(tags, direction)[1] in ALLOW_ACCESS


def _oneway(tags: Mapping[str, str]) -> tuple[str, str]:
    for key in ("oneway:motorcar", "oneway:motor_vehicle", "oneway:vehicle", "oneway"):
        value = _tag(tags, key)
        if value:
            return key, value
    return "", ""


def driving_directions(tags: Mapping[str, str]) -> tuple[bool, bool]:
    """Return (along OSM order, against OSM order), including car overrides."""
    _, oneway = _oneway(tags)
    if oneway in YES:
        forward, backward = True, False
    elif oneway in {"-1", "reverse"}:
        forward, backward = False, True
    elif oneway in NO:
        forward, backward = True, True
    elif oneway:
        return False, False  # reversible/alternating/unknown cannot be assumed legal
    elif _tag(tags, "junction") in {"roundabout", "circular"} or _tag(tags, "highway") == "motorway":
        forward, backward = True, False
    else:
        forward, backward = True, True
    return (forward and car_access_allowed(tags, "forward"),
            backward and car_access_allowed(tags, "backward"))


def is_drivable_way(tags: Mapping[str, str]) -> bool:
    """Ordinary public car roads/links and car ferries; no area or nonroad ways."""
    if _tag(tags, "area") in YES:
        return False
    highway = _tag(tags, "highway")
    ferry = _tag(tags, "route") == "ferry"
    if highway and highway not in ROAD_TYPES:
        return False
    if not ferry and highway not in ROAD_TYPES:
        return False
    return any(driving_directions(tags))


def _conditional_tags(tags: Mapping[str, str]) -> dict[str, str]:
    return {k: str(v) for k, v in sorted(tags.items()) if ":conditional" in k}


def _way_issues(way_id: str, tags: Mapping[str, str], issues: list[dict]) -> None:
    conditional = _conditional_tags(tags)
    if conditional:
        issues.append({"objectType": "way", "id": way_id,
                       "reason": "conditional_rules_not_evaluated", "tags": conditional})
    key, value = _oneway(tags)
    if value and value not in YES | NO | {"-1", "reverse"}:
        issues.append({"objectType": "way", "id": way_id,
                       "reason": "unsupported_oneway_excluded", "tags": {key: value}})
    unknown = {}
    for direction in ("forward", "backward"):
        key, value = _access(tags, direction)
        if value not in ALLOW_ACCESS | DENY_ACCESS:
            unknown[key] = value
    if unknown:
        issues.append({"objectType": "way", "id": way_id,
                       "reason": "unknown_access_excluded", "tags": unknown})


def parse_restriction(
    relation_id: str | int,
    tags: Mapping[str, str],
    members: Iterable[tuple[str, str | int, str]],
    unsupported: list[dict] | None = None,
) -> dict | None:
    """Parse (member type, ref, role) tuples without changing via-way order.

    Same-way from/to IDs are preserved, especially for no_u_turn. Unsupported
    relations retain their tags and members in the supplied diagnostics list.
    Multi-from no_entry and multi-to no_exit are explicitly flagged, not reduced.
    """
    if _tag(tags, "type") != "restriction":
        return None
    exceptions = set(re.split(r"[;,\s]+", _tag(tags, "except")))
    if exceptions & {"motorcar", "motor_vehicle", "vehicle"}:
        return None
    normalized = [(str(kind)[0], str(ref), str(role)) for kind, ref, role in members]

    def flag(reason: str, **extra: object) -> None:
        if unsupported is not None:
            unsupported.append({"id": str(relation_id), "reason": reason,
                                "tags": dict(sorted(tags.items())),
                                "members": [{"type": k, "ref": r, "role": role}
                                            for k, r, role in normalized], **extra})

    conditional = _conditional_tags(tags)
    relevant_conditional = {k: v for k, v in conditional.items()
                            if k in {"restriction:conditional", "restriction:motorcar:conditional",
                                     "restriction:motor_vehicle:conditional", "restriction:vehicle:conditional"}}
    if relevant_conditional:
        flag("conditional_restriction_not_evaluated", conditionalTags=relevant_conditional)
    selected = ""
    for key in ("restriction:motorcar", "restriction:motor_vehicle", "restriction:vehicle", "restriction"):
        selected = _tag(tags, key)
        if selected:
            break
    if not selected:
        return None  # Other modes' restriction relations do not apply to motorcars.
    if selected not in RESTRICTION_TYPES:
        flag("unsupported_restriction_value")
        return None
    from_members = [(k, ref) for k, ref, role in normalized if role == "from"]
    to_members = [(k, ref) for k, ref, role in normalized if role == "to"]
    via_members = [(k, ref) for k, ref, role in normalized if role == "via"]
    if len(from_members) != 1 or len(to_members) != 1:
        flag("multiple_or_missing_from_to_members")
        return None
    if from_members[0][0] != "w" or to_members[0][0] != "w":
        flag("non_way_from_to_member")
        return None
    via_node = None
    via_ways = []
    if len(via_members) == 1 and via_members[0][0] == "n":
        via_node = via_members[0][1]
    elif via_members and all(k == "w" for k, _ in via_members):
        via_ways = [ref for _, ref in via_members]
    else:
        flag("missing_or_mixed_via_members")
        return None
    return {"id": str(relation_id), "fromWay": from_members[0][1],
            "toWay": to_members[0][1], "viaNode": via_node,
            "viaWays": via_ways, "restriction": selected}


def _id_key(value: str) -> tuple:
    return (0, int(value)) if str(value).lstrip("-").isdigit() else (1, str(value))


def _valid_coordinate(point: Sequence[float]) -> bool:
    return (len(point) == 2 and all(math.isfinite(x) for x in point)
            and -180 <= point[0] <= 180 and -90 <= point[1] <= 90)


def _projector() -> Projector:
    from pyproj import Transformer

    return Transformer.from_crs("EPSG:4326", "EPSG:8059", always_xy=True).transform


def build_graph(
    ways: Iterable[RoadWay],
    restrictions: Iterable[dict] = (),
    *,
    project: Projector | None = None,
    metadata: Mapping | None = None,
) -> dict:
    """Build deterministic directed edges; injectable projection enables pure tests.

    No coordinate-based merging occurs. Repeated/shared nodes and restriction
    via-nodes split ways; each directed geometry follows its u -> v direction.
    """
    project = project or _projector()
    meta = dict(metadata or {})
    skipped = meta.setdefault("skippedWays", [])
    invalid = meta.setdefault("invalidCoordinates", [])
    meta.setdefault("missingLocations", [])
    meta.setdefault("unsupportedTags", [])
    unsupported = meta.setdefault("unsupportedRestrictions", [])
    relations = sorted((dict(r) for r in restrictions), key=lambda r: _id_key(r["id"]))
    prepared = []
    uses: Counter = Counter()
    all_positions: dict[str, Coordinate] = {}
    bounds = [math.inf, math.inf, -math.inf, -math.inf]
    for way in sorted(ways, key=lambda w: _id_key(w.id)):
        if len(way.node_ids) != len(way.coordinates) or len(way.node_ids) < 2:
            skipped.append({"wayId": way.id, "reason": "insufficient_or_mismatched_nodes"})
            continue
        bad = [node_id for node_id, point in zip(way.node_ids, way.coordinates)
               if not _valid_coordinate(point)]
        if bad:
            invalid.append({"wayId": way.id, "nodeIds": bad, "reason": "nonfinite_or_out_of_bounds_wgs84"})
            skipped.append({"wayId": way.id, "reason": "invalid_coordinates"})
            continue
        if not way.forward and not way.backward:
            continue
        try:
            xs, ys = project([p[0] for p in way.coordinates], [p[1] for p in way.coordinates])
            if len(xs) != len(way.node_ids) or len(ys) != len(way.node_ids):
                raise ValueError("projection returned the wrong number of coordinates")
            if not all(math.isfinite(x) and math.isfinite(y) for x, y in zip(xs, ys)):
                raise ValueError("projection returned nonfinite coordinates")
            lengths = tuple(math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i])
                            for i in range(len(xs) - 1))
            if not all(math.isfinite(length) for length in lengths):
                raise ValueError("projection produced nonfinite lengths")
        except (ValueError, RuntimeError, ArithmeticError) as exc:
            invalid.append({"wayId": way.id, "reason": "invalid_epsg8059_projection", "detail": str(exc)})
            skipped.append({"wayId": way.id, "reason": "invalid_projection"})
            continue
        for node_id, point in zip(way.node_ids, way.coordinates):
            node_id = str(node_id)
            if node_id in all_positions and all_positions[node_id] != point:
                raise ValueError(f"OSM node {node_id} has inconsistent coordinates")
            all_positions[node_id] = point
            bounds[0] = min(bounds[0], point[0])
            bounds[1] = min(bounds[1], point[1])
            bounds[2] = max(bounds[2], point[0])
            bounds[3] = max(bounds[3], point[1])
        uses.update(str(n) for n in way.node_ids)
        prepared.append((way, lengths))

    split_nodes = {n for n, count in uses.items() if count > 1}
    split_nodes.update(str(r["viaNode"]) for r in relations if r["viaNode"] is not None)
    nodes = {}
    edges = []
    segment_count = 0
    for way, lengths in prepared:
        ids = tuple(str(n) for n in way.node_ids)
        points = [[float(lon), float(lat)] for lon, lat in way.coordinates]
        stops = [0, *(i for i in range(1, len(ids) - 1) if ids[i] in split_nodes), len(ids) - 1]
        for start, end in zip(stops, stops[1:]):
            u, v = ids[start], ids[end]
            nodes[u], nodes[v] = points[start], points[end]
            coordinates = points[start:end + 1]
            length = float(math.fsum(lengths[start:end]))
            segment_count += 1
            for allowed, a, b, shape in (
                (way.forward, u, v, coordinates),
                (way.backward, v, u, list(reversed(coordinates))),
            ):
                if allowed:
                    edges.append({"id": len(edges), "u": a, "v": b, "wayId": str(way.id),
                                  "lengthM": length, "coordinates": shape, "ferry": bool(way.ferry)})

    present_ways = {str(way.id) for way, _ in prepared}
    for relation in relations:
        needed = {relation["fromWay"], relation["toWay"], *relation["viaWays"]}
        missing = sorted(needed - present_ways, key=_id_key)
        if missing:
            unsupported.append({"id": relation["id"], "reason": "referenced_ways_not_in_graph", "wayIds": missing})
        if relation["viaNode"] is not None and relation["viaNode"] not in nodes:
            unsupported.append({"id": relation["id"], "reason": "via_node_not_in_graph", "nodeId": relation["viaNode"]})
    for key in ("unsupportedRestrictions", "unsupportedTags", "missingLocations", "invalidCoordinates", "skippedWays"):
        meta[key].sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    meta.update({
        "schemaVersion": 1, "directed": True, "geometryCrs": "EPSG:4326",
        "lengthCrs": "EPSG:8059", "distanceUnits": "metres", "cost": "distance",
        "bounds": bounds if prepared else None,
        "policy": {"mode": "motorcar", "limitedAccess": "excluded",
                   "conditionalRules": "not evaluated; base tags used",
                   "unknownOneway": "excluded", "areaWays": "excluded",
                   "ferries": "included when car access allows", "topology": "OSM node IDs only"},
        "counts": {**meta.get("counts", {}), "nodes": len(nodes), "edges": len(edges),
                   "ways": len(prepared), "segments": segment_count, "restrictions": len(relations),
                   "ferryEdges": sum(e["ferry"] for e in edges),
                   "unsupportedRestrictions": len(unsupported), "unsupportedTags": len(meta["unsupportedTags"]),
                   "missingLocationWays": len(meta["missingLocations"]),
                   "missingLocationReferences": sum(len(e["nodeIds"]) for e in meta["missingLocations"]),
                   "invalidCoordinateWays": len(invalid), "skippedWays": len(skipped)},
    })
    return {"nodes": {n: nodes[n] for n in sorted(nodes, key=_id_key)},
            "edges": edges, "restrictions": relations, "metadata": meta}


def write_graph(graph: Mapping, output: str | Path) -> Path:
    """Atomically replace JSON only after complete serialization, rejecting NaN."""
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(graph, stream, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target


def extract_network(pbf: str | Path = DEFAULT_PBF, output: str | Path | None = None) -> dict:
    """Stream OSM with flex_mem; store only accepted driving-way locations.

    Returns the graph and optionally writes it atomically. No Overpass calls,
    ArcGIS imports, speed costs, snapping, or routing are performed.
    """
    import osmium

    source = Path(pbf).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"OSM input not found: {source}")
    if output is not None and Path(output).resolve() == source:
        raise ValueError("Output must differ from the OSM input")
    before = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    with osmium.io.Reader(str(source), osmium.osm.NOTHING) as reader:
        header = reader.header()
        if header.has_multiple_object_versions:
            raise ValueError("Historical OSM input is unsupported; use a current snapshot")
        source_header = {key: header.get(key) for key in (
            "generator", "timestamp", "osmosis_replication_timestamp",
            "osmosis_replication_sequence_number", "osmosis_replication_base_url") if header.get(key)}

    meta = {"generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": {"file": source.name, "path": str(source), "sizeBytes": before.st_size,
                       "sha256": digest.hexdigest(),
                       "modifiedAt": datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat(),
                       "timestamp": source_header.get("timestamp") or source_header.get("osmosis_replication_timestamp"),
                       "header": source_header},
            "extractor": {"file": "scripts/extract_network.py", "locationIndex": "flex_mem",
                          "osmium": version("osmium"), "pyproj": version("pyproj")},
            "unsupportedRestrictions": [], "unsupportedTags": [], "missingLocations": [],
            "invalidCoordinates": [], "skippedWays": []}

    class Collector(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways: list[RoadWay] = []
            self.restrictions: list[dict] = []
            self.ways_seen = 0
            self.relations_seen = 0

        def way(self, way):
            self.ways_seen += 1
            tags = {tag.k: tag.v for tag in way.tags}
            if _tag(tags, "highway") not in ROAD_TYPES and _tag(tags, "route") != "ferry":
                return
            way_id = str(way.id)
            _way_issues(way_id, tags, meta["unsupportedTags"])
            if not is_drivable_way(tags):
                return
            ids, coordinates, missing = [], [], []
            for node in way.nodes:
                node_id = str(node.ref)
                ids.append(node_id)
                if not node.location.valid():
                    missing.append(node_id)
                else:
                    coordinates.append((node.lon, node.lat))
            if missing:
                meta["missingLocations"].append({"wayId": way_id, "nodeIds": missing})
                meta["skippedWays"].append({"wayId": way_id, "reason": "missing_locations"})
                return  # Never bridge a missing point with an invented straight segment.
            forward, backward = driving_directions(tags)
            self.ways.append(RoadWay(way_id, tuple(ids), tuple(coordinates), forward, backward,
                                     _tag(tags, "route") == "ferry"))

        def relation(self, relation):
            self.relations_seen += 1
            tags = {tag.k: tag.v for tag in relation.tags}
            if _tag(tags, "type") != "restriction":
                return
            parsed = parse_restriction(relation.id, tags,
                                       ((m.type, m.ref, m.role) for m in relation.members),
                                       meta["unsupportedRestrictions"])
            if parsed is not None:
                self.restrictions.append(parsed)

    collector = Collector()
    collector.apply_file(str(source), locations=True, idx="flex_mem")
    meta["counts"] = {"scannedWays": collector.ways_seen, "scannedRelations": collector.relations_seen}
    graph = build_graph(collector.ways, collector.restrictions, metadata=meta)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("OSM source changed during extraction; output was not written")
    if output is not None:
        write_graph(graph, output)
    return graph


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF, help="Local OSM PBF snapshot")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output graph JSON")
    args = parser.parse_args(argv)
    try:
        graph = extract_network(args.pbf, args.output)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        print(f"Network extraction failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output.resolve()), "sizeBytes": args.output.stat().st_size,
                      **graph["metadata"]["counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
