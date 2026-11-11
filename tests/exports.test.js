import test from 'node:test';
import assert from 'node:assert/strict';
import { aggregateBandCounts } from '../src/analysis.js';
import { summarizeThreshold } from '../src/map-analysis.js';
import { buildSuburbCsv } from '../src/exports.js';

const header = '"Suburb/Locality","Postcode","Members","Road distance (km)","Support group","Analysis mode","Distance threshold (km)"\r\n';
const groups = [{ id: 'north', name: 'Northern group' }, { id: 'south', name: 'Southern group' }];
const options = { thresholdKm: 50, category: 'within', mode: 'group', groups };
const point = (id, members, properties = {}) => ({
  type: 'Feature', geometry: { type: 'Point', coordinates: [138.6, -34.9] },
  properties: { id, name: id, postcode: '5000', members, ...properties },
});
const ok = (distanceM, groupId = 'north') => ({ status: 'ok', distanceM, groupId });
const assignment = (id, members, route = ok(0), properties = {}) => ({
  feature: point(id, members, properties), suburbId: id, members, route,
});
const memberCounts = (csv) => csv.split('\r\n').slice(1, -1)
  .map((line) => Number(line.split(',')[2].slice(1, -1)));

function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

test('inclusive boundary, just-above distance and excluded routes reconcile with threshold totals', () => {
  const assignments = [
    assignment('Origin', 3), assignment('Boundary', 5, ok(50_000), { members: 999 }),
    assignment('Above', 7, ok(50_000.01)), assignment('Unreachable', 11, { status: 'unreachable', distanceM: 0 }),
    assignment('Unsnapped', 13, { status: 'unsnapped' }), assignment('Uncomputed', 17, { status: 'uncomputed' }),
    assignment('Missing', 19, null), assignment('Zero within', 0), assignment('Zero beyond', 0, ok(60_000)),
  ];
  const within = buildSuburbCsv(assignments, options);
  const beyond = buildSuburbCsv(assignments, { ...options, category: 'beyond' });
  assert.equal(within, header
    + '"Boundary","5000","5","50","Northern group","Selected group","50"\r\n'
    + '"Origin","5000","3","0","Northern group","Selected group","50"\r\n');
  assert.equal(beyond, header + '"Above","5000","7","50.00001","Northern group","Selected group","50"\r\n');
  const summary = summarizeThreshold(assignments, 50);
  for (const [category, csv] of [['within', within], ['beyond', beyond]]) {
    const counts = memberCounts(csv);
    assert.deepEqual({ members: counts.reduce((sum, count) => sum + count, 0), suburbs: counts.length }, summary[category]);
  }
  assert.equal(summary.totalMembers, 15 + summary.noRoute.members);
});

test('selected and nearest analysis routes supply each group and update both reports when switched', () => {
  const features = [point('Alpha', 3), point('Beta', 5)];
  const analysis = { groups, distances: {
    Alpha: { north: ok(60_000), south: ok(49_000) },
    Beta: { north: ok(45_000), south: ok(70_000) },
  } };
  const reports = (settings) => {
    const assignments = aggregateBandCounts(features, analysis, settings).assignments;
    return {
      within: buildSuburbCsv(assignments, { ...options, mode: settings.mode }),
      beyond: buildSuburbCsv(assignments, { ...options, mode: settings.mode, category: 'beyond' }),
    };
  };
  assert.deepEqual(reports({ mode: 'group', groupId: 'north' }), {
    within: header + '"Beta","5000","5","45","Northern group","Selected group","50"\r\n',
    beyond: header + '"Alpha","5000","3","60","Northern group","Selected group","50"\r\n',
  });
  assert.deepEqual(reports({ mode: 'nearest' }), {
    within: header + '"Alpha","5000","3","49","Southern group","Nearest group","50"\r\n'
      + '"Beta","5000","5","45","Northern group","Nearest group","50"\r\n',
    beyond: header,
  });
  assert.deepEqual(reports({ mode: 'group', groupId: 'south' }), {
    within: header + '"Alpha","5000","3","49","Southern group","Selected group","50"\r\n',
    beyond: header + '"Beta","5000","5","70","Southern group","Selected group","50"\r\n',
  });
});

test('CSV preserves unicode, precision and leading zeroes while escaping commas, quotes and formulas', () => {
  const csv = buildSuburbCsv([assignment('a', 4, ok(12_345.678), { name: '  =Écho, "SA"', postcode: '0123' })], {
    ...options, thresholdKm: 12.5, groups: [{ id: 'north', name: '@Groupe, "sud" – café' }],
  });
  assert.equal(csv, header + `"'  =Écho, ""SA""","0123","4","12.345678","'@Groupe, ""sud"" – café","Selected group","12.5"\r\n`);
});

test('ordering uses en-AU names, postcodes and suburb IDs, retaining stable ties without mutation', () => {
  const assignments = [
    assignment('z', 7, ok(1000), { name: 'Zulu' }),
    assignment('b', 2, ok(1000), { name: 'Alpha' }),
    assignment('a', 3, ok(1000), { name: 'Alpha' }),
    assignment('q', 5, ok(1000), { name: 'Alpha', postcode: '4000' }),
    assignment('r', 11, ok(1000), { name: 'Éclair' }),
    assignment('s', 13, ok(1000), { name: 'Eden' }),
    assignment('m', 17, ok(1000), { name: 'beta' }),
    assignment('a', 19, ok(1000), { name: 'Alpha' }),
  ];
  const before = structuredClone(assignments);
  freeze(assignments);
  const csv = buildSuburbCsv(assignments, options);
  assert.deepEqual(memberCounts(csv), [5, 3, 19, 2, 17, 11, 13, 7]);
  assert.equal(buildSuburbCsv(assignments, options), csv);
  assert.equal(buildSuburbCsv([...assignments.slice(1), assignments[0]], options), csv);
  assert.deepEqual(assignments, before);
});

test('empty and nonmatching assignments produce only the exact header with CRLF and no BOM', () => {
  assert.equal(buildSuburbCsv([], options), header);
  assert.equal(buildSuburbCsv([], { ...options, category: 'beyond' }), header);
  assert.equal(buildSuburbCsv([assignment('zero', 0), assignment('no-route', 3, null)], options), header);
  assert.equal(buildSuburbCsv([assignment('within', 3)], { ...options, category: 'beyond' }), header);
});

test('unsupported report categories throw RangeError even for empty assignments', () => {
  assert.throws(() => buildSuburbCsv([], { ...options, category: 'all' }), RangeError);
  assert.throws(() => buildSuburbCsv([assignment('a', 3)], { ...options, category: 'no-route' }), RangeError);
});
