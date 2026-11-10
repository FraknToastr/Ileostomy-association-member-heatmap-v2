"""Attach points to an OSM road graph and solve distance-only directed car routes.

Run with the project .venv Python. No ArcGIS Network Analyst licence or online
routing API is needed. Turn restrictions are evaluated on directed edges.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from pyproj import Transformer
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
TO_METRES = Transformer.from_crs(4326, 8059, always_xy=True)
TO_LONLAT = Transformer.from_crs(8059, 4326, always_xy=True)


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def physical_edges(edges):
    """Group opposite directions by complete OSM geometry, never by proximity."""
    physical = []
    lookup = {}
    for edge in edges:
        coords = tuple(tuple(p) for p in edge["coordinates"])
        canonical = min(coords, tuple(reversed(coords)))
        key = (str(edge["wayId"]), canonical)
        if key not in lookup:
            xy = list(zip(*TO_METRES.transform(*zip(*canonical))))
            cumulative = [0.0]
            for a, b in zip(xy, xy[1:]):
                cumulative.append(cumulative[-1] + math.dist(a, b))
            lookup[key] = len(physical)
            physical.append({"coordinates": canonical, "xy": xy,
                             "lengths": cumulative, "cuts": [], "directions": []})
        p = physical[lookup[key]]
        p["directions"].append((edge, coords == canonical))
    return physical


def snap_points(graph, points, maximum_m=5000):
    """Exact closest-segment search in EPSG:8059 with a bounded KD-tree index.

    Sample midpoints at <=1km intervals. Every point on a segment is within
    500m of an indexed sample. Searching best-distance + 500m therefore cannot
    omit a closer road, including a long ferry segment.
    """
    physical = physical_edges(graph["edges"])
    samples, references = [], []
    for pi, p in enumerate(physical):
        for si, (a, b) in enumerate(zip(p["xy"], p["xy"][1:])):
            length = math.dist(a, b)
            steps = max(1, math.ceil(length / 1000))
            for part in range(steps):
                t = (part + .5) / steps
                samples.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
                references.append((pi, si))
    if not samples:
        raise ValueError("The graph has no nonempty driving geometry")
    index = cKDTree(samples)
    snaps = {}

    def project(q, ref):
        pi, si = ref
        p = physical[pi]
        a, b = p["xy"][si:si + 2]
        dx, dy = b[0] - a[0], b[1] - a[1]
        denominator = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / denominator)) if denominator else 0.0
        xy = (a[0] + t * dx, a[1] + t * dy)
        return math.dist(q, xy), p["lengths"][si] + t * math.sqrt(denominator), xy

    for point in points:
        q = TO_METRES.transform(*point["coordinates"])
        _, closest = index.query(q)
        best = project(q, references[int(closest)])
        best_ref = references[int(closest)]
        radius = best[0] + 500.0001
        candidates = {references[i] for i in index.query_ball_point(q, radius)}
        for ref in sorted(candidates):
            candidate = project(q, ref)
            if (candidate[0], ref) < (best[0], best_ref):
                best, best_ref = candidate, ref
        distance, position, xy = best
        snap = {"status": "unsnapped", "distanceM": round(distance, 2), "node": None}
        if distance <= maximum_m:
            pi, _ = best_ref
            p = physical[pi]
            # Positions within one centimetre deliberately share a graph node.
            position = max(0, min(p["lengths"][-1], round(position, 2)))
            first, forward = p["directions"][0]
            start = str(first["u"] if forward else first["v"])
            end = str(first["v"] if forward else first["u"])
            if position < .01:
                position, node = 0.0, start
            elif p["lengths"][-1] - position < .01:
                position, node = p["lengths"][-1], end
            else:
                node = f"snap-{pi}-{position:.2f}"
            p["cuts"].append((position, node))
            snap.update(status="review" if distance > 1000 else "ok", node=node,
                        coordinates=list(TO_LONLAT.transform(*xy)))
        snaps[point["id"]] = snap

    edges = []
    for p in physical:
        for original, forward in p["directions"]:
            start = str(original["u"] if forward else original["v"])
            end = str(original["v"] if forward else original["u"])
            cuts = sorted(set([(0.0, start), (p["lengths"][-1], end)] + p["cuts"]))
            for (lo, u), (hi, v) in zip(cuts, cuts[1:]):
                if hi - lo < .001:
                    continue
                xy = [interpolate(p, lo)]
                xy += [point for length, point in zip(p["lengths"], p["xy"]) if lo < length < hi]
                xy += [interpolate(p, hi)]
                coords = [list(TO_LONLAT.transform(*point)) for point in xy]
                edges.append({"id": len(edges), "u": u if forward else v, "v": v if forward else u,
                              "wayId": str(original["wayId"]), "lengthM": hi - lo,
                              "coordinates": coords if forward else list(reversed(coords)),
                              "ferry": bool(original.get("ferry", False))})
    return {**graph, "edges": edges}, snaps


def interpolate(physical, offset):
    import bisect
    i = min(max(0, bisect.bisect_right(physical["lengths"], offset) - 1), len(physical["xy"]) - 2)
    a, b = physical["xy"][i:i + 2]
    length = physical["lengths"][i + 1] - physical["lengths"][i]
    t = (offset - physical["lengths"][i]) / length if length else 0
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


class TurnRules:
    """Node restrictions plus a reverse finite-state matcher for via-way rules."""
    def __init__(self, edges, restrictions):
        self.node_rules = defaultdict(list)
        self.via_rules = defaultdict(list)
        way_nodes = defaultdict(set)
        self.patterns = []
        for e in edges:
            way_nodes[str(e["wayId"])].update((str(e["u"]), str(e["v"])))
        for r in restrictions:
            r = {**r, "fromWay": str(r["fromWay"]), "toWay": str(r["toWay"])}
            if r.get("viaNode") is not None:
                self.node_rules[(str(r["viaNode"]), r["fromWay"])].append(r)
            elif r.get("viaWays"):
                seq = tuple([r["fromWay"]] + [str(v) for v in r["viaWays"]] + [r["toWay"]])
                reversed_seq = tuple(reversed(seq))
                r["patterns"] = (reversed_seq,)
                self.patterns.append(reversed_seq)
                if r["restriction"].startswith("only_"):
                    # None anchors an incomplete required sequence to the target.
                    # An unanchored prefix would also allow an early exit.
                    partials = tuple((None,) + tuple(reversed(seq[:i])) for i in range(2, len(seq)))
                    r["patterns"] += partials
                    self.patterns.extend(partials)
                for node in way_nodes[seq[0]] & way_nodes[seq[1]]:
                    self.via_rules[(node, seq[0])].append(r)
        prefixes = {()}
        for pattern in self.patterns:
            prefixes.update(pattern[:i] for i in range(1, len(pattern) + 1))
        self.prefixes = sorted(prefixes, key=lambda p: (len(p), tuple(map(str, p))))
        self.ids = {p: i for i, p in enumerate(self.prefixes)}
        self.target_state = self.ids.get((None,), 0)
        self.prefix_set = prefixes
        self.transitions = {}

    def advance(self, state, way, same_way=False):
        if not self.patterns:
            return 0, ()
        if same_way:
            return state, ()
        key = (state, way)
        if key in self.transitions:
            return self.transitions[key]
        candidate = self.prefixes[state] + (way,)
        matched = tuple(p for p in self.patterns if len(p) <= len(candidate) and candidate[-len(p):] == p)
        while candidate not in self.prefix_set:
            candidate = candidate[1:]
        result = self.ids[candidate], matched
        self.transitions[key] = result
        return result

    def transition(self, incoming, outgoing, state):
        """Return next reverse matcher state, or None for an illegal turn."""
        from_way, to_way = str(incoming["wayId"]), str(outgoing["wayId"])
        for r in self.node_rules.get((str(incoming["v"]), from_way), ()):
            hit = to_way == r["toWay"]
            if r["restriction"].endswith("u_turn") and r["fromWay"] == r["toWay"]:
                hit = hit and incoming["u"] == outgoing["v"]
            elif r["restriction"] == "only_straight_on" and r["fromWay"] == r["toWay"]:
                hit = hit and incoming["u"] != outgoing["v"]
            if (r["restriction"].startswith("no_") and hit) or (r["restriction"].startswith("only_") and not hit):
                return None
        new_state, matched = self.advance(state, from_way, from_way == to_way)
        for r in self.via_rules.get((str(incoming["v"]), from_way), ()):
            hit = any(pattern in matched for pattern in r["patterns"])
            if (r["restriction"].startswith("no_") and hit) or (r["restriction"].startswith("only_") and not hit):
                return None
        return new_state


def shortest_routes(graph, target, origins):
    """Reverse edge-state Dijkstra: 11 solves instead of 1,895 source solves."""
    edges = graph["edges"]
    incoming = defaultdict(list)
    for i, e in enumerate(edges):
        incoming[str(e["v"])].append(i)
    rules = TurnRules(edges, graph.get("restrictions", []))
    states = len(rules.prefixes)
    costs, successor = {}, {}
    heap = []
    for ei in incoming.get(str(target), []):
        e = edges[ei]
        state, _ = rules.advance(rules.target_state, str(e["wayId"]))
        key = ei * states + state
        costs[key] = e["lengthM"]
        successor[key] = None
        heapq.heappush(heap, (costs[key], key))
    pending = set(str(o) for o in origins if o is not None)
    results = {}
    if str(target) in pending:
        pending.remove(str(target))
        results[str(target)] = (0.0, None)
    while heap and pending:
        distance, key = heapq.heappop(heap)
        if distance != costs.get(key):
            continue
        ei, state = divmod(key, states)
        e = edges[ei]
        node = str(e["u"])
        if node in pending:
            pending.remove(node)
            results[node] = (distance, key)
        for previous in incoming.get(node, ()):
            a = edges[previous]
            new_state = rules.transition(a, e, state)
            if new_state is None:
                continue
            previous_key = previous * states + new_state
            candidate = distance + a["lengthM"]
            if candidate < costs.get(previous_key, math.inf):
                costs[previous_key] = candidate
                successor[previous_key] = key
                heapq.heappush(heap, (candidate, previous_key))
    return results, successor, states


def simplify(points, tolerance=20):
    """Douglas-Peucker in projected metres, display only; costs stay exact."""
    if len(points) < 3:
        return points
    xy = list(zip(*TO_METRES.transform(*zip(*points))))
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        a, b = xy[first], xy[last]
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = dx * dx + dy * dy
        maximum, split = 0, first
        for i in range(first + 1, last):
            q = xy[i]
            t = max(0, min(1, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / norm)) if norm else 0
            d = (q[0] - a[0] - t * dx) ** 2 + (q[1] - a[1] - t * dy) ** 2
            if d > maximum:
                maximum, split = d, i
        if maximum > tolerance * tolerance:
            keep.add(split)
            stack.extend(((first, split), (split, last)))
    return [[round(float(v), 6) for v in points[i]] for i in sorted(keep)]


def init_routing_worker(graph, snaps, suburb_ids, outdir):
    global WORKER_GRAPH, WORKER_SNAPS, WORKER_SUBURBS, WORKER_OUTDIR
    WORKER_GRAPH, WORKER_SNAPS, WORKER_SUBURBS, WORKER_OUTDIR = graph, snaps, suburb_ids, outdir


def solve_group(group):
    graph, snaps = WORKER_GRAPH, WORKER_SNAPS
    gid = group['id']
    target = snaps[gid]['node']
    origins = [snaps[sid]['node'] for sid in WORKER_SUBURBS]
    print(f"Routing {group['name']}", flush=True)
    solved, successor, states = shortest_routes(graph, target, origins) if target else ({}, {}, 1)
    features, entries = [], {}
    for sid in WORKER_SUBURBS:
        origin = snaps[sid]['node']
        entry = {'status': 'unsnapped' if origin is None or target is None else 'unreachable',
                 'distanceM': None, 'usesFerry': False}
        if origin in solved:
            length, key = solved[origin]
            coords, ferry = [], False
            while key is not None:
                edge = graph['edges'][key // states]
                ferry |= edge['ferry']
                coords.extend(edge['coordinates'] if not coords else edge['coordinates'][1:])
                key = successor[key]
            entry.update(status='ok', distanceM=round(length, 2), usesFerry=ferry)
            if not coords:
                coords = [snaps[sid]['coordinates'], snaps[sid]['coordinates']]
            features.append({'type': 'Feature', 'properties': {'suburbId': sid, 'distanceM': entry['distanceM'], 'usesFerry': ferry},
                             'geometry': {'type': 'LineString', 'coordinates': simplify(coords)}})
        entries[sid] = entry
    write_json(WORKER_OUTDIR / 'routes' / f'{gid}.geojson', {'type': 'FeatureCollection', 'features': features})
    print(f"  {group['name']}: {len(features)} reachable suburbs", flush=True)
    return gid, entries


def build_distances(graph_path, members_path, groups_path, outdir, workers=4):
    graph = read_json(graph_path)
    members = read_json(members_path)["features"]
    groups_source = read_json(groups_path)["features"]
    groups = [{"id": f"group-{f['properties']['OBJECTID']}",
               "name": f["properties"].get("Name", "Support group"),
               "coordinates": f["geometry"]["coordinates"][:2]} for f in groups_source]
    points = [{"id": f["properties"]["id"], "coordinates": f["geometry"]["coordinates"][:2]} for f in members] + groups
    print(f"Snapping {len(points)} points to {len(graph['edges'])} directed edges", flush=True)
    graph, snaps = snap_points(graph, points)
    distances = {p["id"]: {} for p in points[:-len(groups)]}
    snap_review = []
    for p in points:
        if snaps[p["id"]]["status"] != "ok":
            snap_review.append({"id": p["id"], **snaps[p["id"]]})
    for group in groups:
        group['snap'] = snaps[group['id']]
    worker_args = (graph, snaps, list(distances), outdir)
    if workers == 1:
        init_routing_worker(*worker_args)
        group_results = map(solve_group, groups)
        for gid, entries in group_results:
            for sid, entry in entries.items():
                distances[sid][gid] = entry
    else:
        # Fork shares the read-only geometry on Linux; spawn also supports Windows.
        context = multiprocessing.get_context('fork' if 'fork' in multiprocessing.get_all_start_methods() else 'spawn')
        with ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                 initializer=init_routing_worker, initargs=worker_args) as pool:
            for gid, entries in pool.map(solve_group, groups):
                for sid, entry in entries.items():
                    distances[sid][gid] = entry
    source_timestamp = graph.get('metadata', {}).get('source', {}).get('timestamp')
    extraction_metadata = dict(graph.get('metadata', {}))
    if isinstance(extraction_metadata.get('source'), dict):
        extraction_metadata['source'] = {k: v for k, v in extraction_metadata['source'].items() if k != 'path'}
    metadata = {
        "sourceDate": str(source_timestamp)[:10] if source_timestamp else 'Unknown', "membershipDate": "2026-10",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "boundaryWarning": "Routes use the supplied South Australian OSM extract only. A shorter route outside this extract may exist; cross-border travel is not modelled.",
        "network": {"nodes": len({e['u'] for e in graph['edges']} | {e['v'] for e in graph['edges']}), "edges": len(graph["edges"])},
        "snapReviewCount": len(snap_review),
        "restrictionWarnings": graph.get("metadata", {}).get("unsupportedRestrictions", []),
        "extraction": extraction_metadata,
        "routeDirection": "suburb-to-group", "impedance": "distance-metres",
        "displaySimplificationM": 20,
        "snapPolicy": {"maximumM": 5000, "reviewAboveM": 1000, "offsetIncludedInDistance": False},
    }
    write_json(outdir / "analysis.json", {"schemaVersion": 1, "groups": groups, "distances": distances, "metadata": metadata})
    write_json(outdir / "snap-report.json", {"review": snap_review, "snaps": snaps})
    manifest = {"schemaVersion": 1, "generatedAt": metadata["generatedAt"], "routing": metadata,
                "sources": [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in [members_path, groups_path]],
                "files": ["members.geojson", "membership-report.json", "analysis.json", "snap-report.json"] + [f"routes/{g['id']}.geojson" for g in groups]}
    write_json(outdir / "manifest.json", manifest)
    return distances


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=ROOT / ".generated/network.json")
    parser.add_argument("--members", type=Path, default=ROOT / "public/data/members.geojson")
    parser.add_argument("--groups", type=Path, default=ROOT / "Ileo_Support_Groups.geojson")
    parser.add_argument("--output", type=Path, default=ROOT / "public/data")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9), help='Parallel group solves (1–8); use 1 on memory-limited hosts')
    args = parser.parse_args()
    build_distances(args.graph, args.members, args.groups, args.output, args.workers)


if __name__ == "__main__":
    main()
