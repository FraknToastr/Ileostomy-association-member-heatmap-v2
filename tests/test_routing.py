import unittest
import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from route_distances import shortest_routes, snap_points, TO_LONLAT


def edge(u, v, length, way, coords=None):
    return {'u': str(u), 'v': str(v), 'lengthM': length, 'wayId': str(way),
            'coordinates': coords or [[138, -34], [138.001, -34]], 'ferry': False}


def distance(edges, start, end, restrictions=()):
    graph = {'edges': edges, 'restrictions': restrictions}
    routes, following, states = shortest_routes(graph, str(end), [str(start)])
    if str(start) not in routes:
        return None
    length, key = routes[str(start)]
    reconstructed = 0
    visited = set()
    while key is not None:
        if key in visited:
            raise AssertionError('Cyclic reconstruction')
        visited.add(key)
        reconstructed += edges[key // states]['lengthM']
        key = following[key]
    assert abs(length - reconstructed) < 1e-6
    return length


class DirectedDistanceTests(unittest.TestCase):
    def test_directed_distance_minimizes_metres(self):
        roads = [edge('a', 'b', 3, 1), edge('b', 'c', 4, 2), edge('a', 'c', 20, 3)]
        self.assertEqual(distance(roads, 'a', 'c'), 7)
        self.assertIsNone(distance(roads, 'c', 'a'))

    def test_node_turn_restriction_forces_detour(self):
        roads = [edge('a', 'b', 3, 1), edge('b', 'c', 4, 2), edge('a', 'c', 20, 3)]
        rule = {'fromWay': '1', 'viaNode': 'b', 'toWay': '2', 'restriction': 'no_right_turn'}
        self.assertEqual(distance(roads, 'a', 'c', [rule]), 20)

    def test_only_turn_disallows_cheaper_alternative(self):
        roads = [edge('a', 'b', 1, 1), edge('b', 'c', 1, 2), edge('b', 'd', 3, 3), edge('d', 'c', 3, 4)]
        rule = {'fromWay': '1', 'viaNode': 'b', 'toWay': '3', 'restriction': 'only_right_turn'}
        self.assertEqual(distance(roads, 'a', 'c', [rule]), 7)

    def test_via_way_no_turn_and_only_turn(self):
        roads = [edge('a', 'b', 1, 1), edge('b', 'v', 1, 2), edge('v', 'c', 1, 3),
                 edge('v', 'd', 5, 4), edge('d', 'c', 5, 5)]
        rule = {'fromWay': '1', 'viaNode': None, 'viaWays': ['2'], 'toWay': '3', 'restriction': 'no_straight_on'}
        self.assertEqual(distance(roads, 'a', 'c', [rule]), 12)
        rule['restriction'] = 'only_straight_on'
        self.assertEqual(distance(roads, 'a', 'c', [rule]), 3)
        self.assertIsNone(distance(roads, 'a', 'd', [rule]))

    def test_via_way_rule_survives_split_edges(self):
        roads = [edge('a', 'b', 1, 1), edge('b', 'snap', 1, 2), edge('snap', 'v', 1, 2), edge('v', 'c', 1, 3)]
        rule = {'fromWay': '1', 'viaNode': None, 'viaWays': ['2'], 'toWay': '3', 'restriction': 'no_straight_on'}
        self.assertIsNone(distance(roads, 'a', 'c', [rule]))
        # Starting on the via way has not traversed the prohibited from way.
        self.assertEqual(distance(roads, 'snap', 'c', [rule]), 2)

    def test_no_u_turn_does_not_ban_continuing_same_way(self):
        roads = [edge('a', 'b', 1, 10), edge('b', 'a', 1, 10), edge('b', 'c', 1, 10)]
        rule = {'fromWay': '10', 'viaNode': 'b', 'toWay': '10', 'restriction': 'no_u_turn'}
        self.assertEqual(distance(roads, 'a', 'c', [rule]), 2)

    def test_only_via_way_allows_target_on_split_via_but_not_early_exit(self):
        roads = [edge('a', 'b', 1, 1), edge('b', 'snap', 1, 2),
                 edge('snap', 'v', 1, 2), edge('v', 'c', 1, 3),
                 edge('snap', 'exit', .1, 4), edge('exit', 'd', .1, 2),
                 edge('d', 'c', .1, 3)]
        rule = {'fromWay': '1', 'viaWays': ['2'], 'toWay': '3', 'restriction': 'only_straight_on'}
        for target, expected in [('b', 1), ('snap', 2), ('v', 3), ('c', 4)]:
            self.assertEqual(distance(roads, 'a', target, [rule]), expected, target)
        for target in ['exit', 'd']:
            self.assertIsNone(distance(roads, 'a', target, [rule]), target)
        self.assertEqual(distance(roads, 'snap', 'exit', [rule]), .1)

    def test_only_multiple_via_ways_allows_each_terminal_prefix(self):
        roads = [edge('a', 'b', 1, 1), edge('b', 'c', 1, 2),
                 edge('c', 'snap', 1, 3), edge('snap', 'd', 1, 3),
                 edge('d', 'e', 1, 4), edge('c', 'early', 1, 5),
                 edge('d', 'wrong', 1, 6)]
        rule = {'fromWay': '1', 'viaWays': ['2', '3'], 'toWay': '4', 'restriction': 'only_straight_on'}
        for target, expected in [('c', 2), ('snap', 3), ('d', 4), ('e', 5)]:
            self.assertEqual(distance(roads, 'a', target, [rule]), expected, target)
        for target in ['early', 'wrong']:
            self.assertIsNone(distance(roads, 'a', target, [rule]), target)
        rule['restriction'] = 'no_straight_on'
        self.assertEqual(distance(roads, 'a', 'snap', [rule]), 3)
        self.assertIsNone(distance(roads, 'a', 'e', [rule]))

    def test_coincident_geometry_does_not_connect_bridge_nodes(self):
        roads = [edge('a', 'b', 1, 1), edge('c', 'd', 1, 2)]
        self.assertIsNone(distance(roads, 'a', 'd'))

    def test_origin_equals_destination_is_zero(self):
        self.assertEqual(distance([edge('a', 'b', 1, 1)], 'b', 'b'), 0)

    def test_random_graph_costs_agree_with_independent_bellman_ford(self):
        rng = random.Random(20261109)
        for trial in range(12):
            roads = [edge(u, v, rng.randint(1, 100), i)
                     for i, (u, v) in enumerate((u, v) for u in range(8) for v in range(8) if u != v)
                     if rng.random() < .22]
            target = '7'
            costs = {str(i): float('inf') for i in range(8)}
            costs[target] = 0
            for _ in range(7):
                for road in roads:
                    costs[road['u']] = min(costs[road['u']], road['lengthM'] + costs[road['v']])
            for start in map(str, range(8)):
                expected = costs[start] if costs[start] != float('inf') else None
                self.assertEqual(distance(roads, start, target), expected, (trial, start))


class SnappingTests(unittest.TestCase):
    def test_exact_edge_split_and_directions(self):
        a = list(TO_LONLAT.transform(1000000, 1800000))
        b = list(TO_LONLAT.transform(1001000, 1800000))
        midpoint = list(TO_LONLAT.transform(1000500, 1800100))
        graph, snaps = snap_points({'edges': [edge('a', 'b', 1000, 1, [a, b])]}, [
            {'id': 's', 'coordinates': midpoint}, {'id': 'g', 'coordinates': b}])
        self.assertAlmostEqual(snaps['s']['distanceM'], 100, places=1)
        self.assertAlmostEqual(distance(graph['edges'], snaps['s']['node'], snaps['g']['node']), 500, places=1)
        self.assertIsNone(distance(graph['edges'], snaps['g']['node'], snaps['s']['node']))

    def test_over_limit_is_not_snapped(self):
        a = list(TO_LONLAT.transform(1000000, 1800000))
        b = list(TO_LONLAT.transform(1001000, 1800000))
        far = list(TO_LONLAT.transform(1000500, 1810000))
        _, snaps = snap_points({'edges': [edge('a', 'b', 1000, 1, [a, b])]}, [{'id': 's', 'coordinates': far}])
        self.assertEqual(snaps['s']['status'], 'unsnapped')
        self.assertIsNone(snaps['s']['node'])

    def test_endpoint_snaps_reuse_nodes_without_duplicate_cuts(self):
        a = list(TO_LONLAT.transform(1000000, 1800000))
        b = list(TO_LONLAT.transform(1000123.453, 1800000))
        roads = [edge('a', 'b', 123.453, 1, [a, b]),
                 edge('b', 'a', 123.453, 1, [b, a])]
        graph, snaps = snap_points({'edges': roads}, [
            {'id': 'start', 'coordinates': a}, {'id': 'end', 'coordinates': b}])
        self.assertEqual([snaps[p]['node'] for p in ['start', 'end']], ['a', 'b'])
        self.assertEqual(len(graph['edges']), 2)
        self.assertTrue(all(e['u'] != e['v'] for e in graph['edges']))
        self.assertAlmostEqual(distance(graph['edges'], 'a', 'b'), 123.453, places=5)
        self.assertAlmostEqual(distance(graph['edges'], 'b', 'a'), 123.453, places=5)


if __name__ == '__main__':
    unittest.main()
