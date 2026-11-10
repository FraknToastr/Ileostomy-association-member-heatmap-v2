import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const read = (name) => JSON.parse(fs.readFileSync(new URL(`../public/data/${name}`, import.meta.url), 'utf8'));

test('published member totals reconcile and every suburb/group pair has a routing status', () => {
  const suburbs = read('members.geojson').features;
  const report = read('membership-report.json');
  const analysis = read('analysis.json');
  const joined = suburbs.reduce((sum, f) => sum + f.properties.members, 0);
  assert.equal(joined, report.totals.joinedMembers);
  assert.equal(joined + report.totals.unresolvedMembers, report.totals.memberRecords);
  assert.equal(report.unresolved.reduce((sum, r) => sum + r.count, 0), report.totals.unresolvedMembers);
  const groups = analysis.groups.map(g => g.id).sort();
  assert.equal(Object.keys(analysis.distances).length, suburbs.length);
  assert.equal(new Set(suburbs.map(f => f.properties.id)).size, suburbs.length);
  for (const { properties } of suburbs) {
    assert.ok(Number.isInteger(properties.members) && properties.members >= 0);
    const row = analysis.distances[properties.id];
    assert.deepEqual(Object.keys(row).sort(), groups);
    for (const result of Object.values(row)) {
      if (result.status === 'ok') assert.ok(Number.isFinite(result.distanceM) && result.distanceM >= 0);
      else {
        assert.ok(['unsnapped', 'unreachable'].includes(result.status));
        assert.equal(result.distanceM, null);
      }
    }
  }
});

test('each group route file contains exactly its reachable suburbs and the same metre costs', () => {
  const analysis = read('analysis.json');
  for (const group of analysis.groups) {
    const routes = read(`routes/${group.id}.geojson`).features;
    const expected = Object.entries(analysis.distances).filter(([, row]) => row[group.id].status === 'ok').map(([sid]) => sid).sort();
    assert.deepEqual(routes.map(f => f.properties.suburbId).sort(), expected);
    for (const route of routes) {
      assert.equal(route.geometry.type, 'LineString');
      assert.ok(route.geometry.coordinates.length >= 2);
      assert.equal(route.properties.distanceM, analysis.distances[route.properties.suburbId][group.id].distanceM);
    }
  }
});
