import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeDistanceSettings, chooseRoute, aggregateBandCounts, distanceBandIndex, filterAssignments, buildBandCsv, csvCell } from '../src/analysis.js';

const groups = [{ id: 'group-2', name: 'Two' }, { id: 'group-1', name: 'One' }];
const point = (id, members) => ({ type: 'Feature', properties: { id, members }, geometry: { type: 'Point', coordinates: [138.6, -34.9] } });
const ok = (distanceM, usesFerry = false) => ({ status: 'ok', distanceM, usesFerry });
const features = [point('a', 3), point('b', 5), point('c', 7), point('d', 11), point('e', 13), point('f', 17), point('g', 19), point('h', 23)];
const analysis = { groups, distances: {
  a: { 'group-1': ok(0) }, b: { 'group-1': ok(24999.99) }, c: { 'group-1': ok(25000) }, d: { 'group-1': ok(50000) },
  e: { 'group-1': ok(100000) }, f: { 'group-1': ok(200000, true) }, g: { 'group-1': { status: 'unreachable' } }, h: { 'group-1': { status: 'unsnapped' } },
} };

test('zero-member centroids remain available without inflating member-bearing suburb counts', () => {
  const result = aggregateBandCounts([point('a', 3), point('b', 0)], analysis, { mode: 'nearest' });
  assert.equal(result.bands[0].members, 3);
  assert.equal(result.bands[0].suburbs, 1);
  assert.equal(result.assignments.length, 2);
});

test('settings accept defaults and decimal ranges', () => {
  assert.deepEqual(normalizeDistanceSettings().boundariesKm, [25, 50, 100, 200]);
  assert.equal(normalizeDistanceSettings({ boundariesKm: '10, 20.5, 100', minKm: '0', maxKm: '50' }).valid, true);
  assert.equal(normalizeDistanceSettings({ minKm: '', maxKm: ' ' }).maxKm, null);
});
test('settings reject nonfinite, nonpositive, duplicate and unordered boundaries', () => {
  for (const boundariesKm of ['', [], '25,,50', [0, 25], [-1, 25], [25, 25], [50, 25], [25, Infinity], [true, 25], [' ', 25], 'NaN']) assert.equal(normalizeDistanceSettings({ boundariesKm }).valid, false);
  for (const range of [{ minKm: -1 }, { maxKm: Infinity }, { minKm: 50, maxKm: 50 }, { minKm: 51, maxKm: 50 }]) assert.equal(normalizeDistanceSettings(range).valid, false);
});
test('bands use lower inclusive and upper exclusive boundaries in metres', () => {
  assert.deepEqual([0, 24999.99, 25000, 50000, 100000, 200000, 1e6].map((m) => distanceBandIndex(m)), [0, 0, 1, 2, 3, 4, 4]);
  assert.equal(distanceBandIndex(null), -1);
  assert.equal(distanceBandIndex(-1), -1);
});
test('absolute member and suburb counts preserve the full joined denominator', () => {
  const s = aggregateBandCounts(features, analysis, { groupId: 'group-1', joinedMembers: 98 });
  assert.deepEqual(s.bands.map((b) => b.members), [8, 7, 11, 13, 17]);
  assert.deepEqual(s.bands.map((b) => b.suburbs), [2, 1, 1, 1, 1]);
  assert.equal(s.unreachable.members, 19);
  assert.equal(s.unsnapped.members, 23);
  assert.equal(s.uncomputed.members, 0);
  assert.equal(s.totals.memberSum, 98);
  assert.equal(s.bands[0].percentage, 8 / 98 * 100);
  assert.equal(s.denominatorMismatch, false);
  assert.equal([...s.bands, s.unreachable, s.unsnapped, s.uncomputed].reduce((n, b) => n + b.members, 0), 98);
});
test('nearest ties use stable IDs, retain ferry flags and never mutate group order', () => {
  const r = chooseRoute('a', groups, { a: { 'group-1': ok(1000, true), 'group-2': ok(1000) } }, { mode: 'nearest' });
  assert.equal(r.groupId, 'group-1');
  assert.equal(r.distanceM, 1000);
  assert.equal(r.usesFerry, true);
  assert.equal(groups[0].id, 'group-2');
});
test('missing or invalid results are uncomputed, never unreachable', () => {
  const distances = { a: { 'group-1': { status: 'unreachable' } } };
  assert.equal(chooseRoute('a', groups, distances, { mode: 'nearest' }).status, 'uncomputed');
  assert.equal(chooseRoute('a', groups, distances, { groupId: 'group-2' }).status, 'uncomputed');
  assert.equal(chooseRoute('a', groups, { a: { 'group-1': ok(null) } }, { groupId: 'group-1' }).status, 'uncomputed');
  assert.equal(chooseRoute('a', [], {}, { mode: 'nearest' }).status, 'uncomputed');
  const s = aggregateBandCounts([point('missing', 9)], analysis, { groupId: 'group-1' });
  assert.equal(s.uncomputed.members, 9);
  assert.equal(s.unreachable.members, 0);
});
test('nearest distinguishes snapping, all unreachable and incomplete candidate sets', () => {
  assert.equal(chooseRoute('a', groups, { a: { 'group-1': { status: 'unreachable' }, 'group-2': { status: 'unsnapped' } } }, { mode: 'nearest' }).status, 'unsnapped');
  assert.equal(chooseRoute('a', groups, { a: { 'group-1': { status: 'unreachable' }, 'group-2': { status: 'unreachable' } } }, { mode: 'nearest' }).status, 'unreachable');
  assert.equal(chooseRoute('a', groups, { a: { 'group-1': ok(100) } }, { mode: 'nearest' }).incomplete, true);
});
test('display filters preserve base totals and implement [min, max)', () => {
  const s = aggregateBandCounts(features, analysis, { groupId: 'group-1' });
  assert.deepEqual(filterAssignments(s.assignments, { minKm: 25, maxKm: 100 }).map((v) => v.suburbId), ['c', 'd']);
  assert.equal(filterAssignments(s.assignments, { bandId: 'unreachable' })[0].members, 19);
  assert.equal(filterAssignments(s.assignments).length, 8);
  assert.equal(filterAssignments(s.assignments, { bandId: 'band-0', minKm: 25 }).length, 0);
  assert.equal(s.totals.memberSum, 98);
});
test('zero denominator percentages stay finite and mismatches are explicit', () => {
  const s = aggregateBandCounts([], analysis, { joinedMembers: 0 });
  assert.ok([...s.bands, s.unreachable, s.unsnapped, s.uncomputed].every((b) => b.percentage === 0));
  assert.equal(aggregateBandCounts(features, analysis, { groupId: 'group-1', joinedMembers: 0 }).denominatorMismatch, true);
});
test('CSV includes absolute counts and mitigates spreadsheet formula injection', () => {
  const csv = buildBandCsv(aggregateBandCounts(features, analysis, { groupId: 'group-1' }), { groupName: '  =HYPERLINK("bad")' });
  assert.ok(csv.includes('"\'  =HYPERLINK(""bad"")"'));
  assert.ok(csv.includes('"Unreachable","","","19","1"'));
  for (const v of ['=1', '+1', '-1', '@SUM(A1)', '\t=1', '\n@x', '  +1']) assert.ok(csvCell(v).startsWith('"\''));
  assert.equal(csvCell('Central, "SA"'), '"Central, ""SA"""');
});
