"""Validate publishable aggregates and routing outputs without source records."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(directory):
    directory = Path(directory)
    load = lambda name: json.loads((directory / name).read_text(encoding='utf-8'))
    members = load('members.geojson')['features']
    report = load('membership-report.json')
    analysis = load('analysis.json')
    totals = report['totals']
    ids = [f['properties']['id'] for f in members]
    assert len(set(ids)) == len(ids), 'Duplicate suburb IDs'
    assert sum(f['properties']['members'] for f in members) == totals['joinedMembers'], 'Joined counts do not reconcile'
    assert totals['joinedMembers'] + totals['unresolvedMembers'] == totals['memberRecords'], 'Source counts do not reconcile'
    assert sum(r['count'] for r in report['unresolved']) == totals['unresolvedMembers'], 'Unresolved counts do not reconcile'
    group_ids = [g['id'] for g in analysis['groups']]
    assert len(group_ids) == len(set(group_ids)) > 0, 'Invalid group IDs'
    assert set(analysis['distances']) == set(ids), 'Missing/extra routing origins'
    reachable = 0
    expected_routes = {gid: {} for gid in group_ids}
    for sid, row in analysis['distances'].items():
        assert set(row) == set(group_ids), f'Incomplete group matrix: {sid}'
        for gid, entry in row.items():
            assert entry['status'] in {'ok', 'unsnapped', 'unreachable'}, entry
            if entry['status'] == 'ok':
                assert isinstance(entry['distanceM'], (float, int)) and entry['distanceM'] >= 0
                reachable += 1
                expected_routes[gid][sid] = entry
            else:
                assert entry['distanceM'] is None, 'Failed routes must not have substitute distances'
    for gid in group_ids:
        routes = load(f'routes/{gid}.geojson')['features']
        seen = set()
        for route in routes:
            props = route['properties']
            sid = props['suburbId']
            assert sid not in seen, f'Duplicate route {sid}/{gid}'
            seen.add(sid)
            assert props['distanceM'] == expected_routes[gid][sid]['distanceM'], 'Route/matrix cost mismatch'
            assert route['geometry']['type'] == 'LineString' and len(route['geometry']['coordinates']) >= 2
            assert all(-180 <= p[0] <= 180 and -90 <= p[1] <= 90 for p in route['geometry']['coordinates'])
        assert seen == set(expected_routes[gid]), f'Missing display routes: {gid}'
    # Check the member-count invariant for every group and nearest-group mode.
    summaries = {}
    for gid in group_ids + ['nearest']:
        bands = [0] * 5
        unreachable = 0
        for feature in members:
            props = feature['properties']
            row = analysis['distances'][props['id']]
            options = [(entry['distanceM'], key) for key, entry in row.items() if entry['status'] == 'ok']
            entry = row[gid] if gid != 'nearest' else row[min(options)[1]] if options else None
            if not entry or entry['status'] != 'ok':
                unreachable += props['members']
            else:
                band = sum(entry['distanceM'] >= limit for limit in [25000, 50000, 100000, 200000])
                bands[band] += props['members']
        assert sum(bands) + unreachable == totals['joinedMembers']
        summaries[gid] = {'bands': bands, 'unreachable': unreachable}
    return {'suburbs': len(members), 'groups': len(group_ids), 'joinedMembers': totals['joinedMembers'],
            'unresolvedMembers': totals['unresolvedMembers'], 'reachablePairs': reachable, 'summaries': summaries}


if __name__ == '__main__':
    print(json.dumps(validate(ROOT / 'public/data'), indent=2))
