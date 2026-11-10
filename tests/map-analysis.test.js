import test from 'node:test';
import assert from 'node:assert/strict';
import { aggregateBandCounts, filterAssignments } from '../src/analysis.js';
import { thresholdStatus, summarizeThreshold, prepareMapFeatures, clusterProperties, clusterSummary } from '../src/map-analysis.js';

const point = (id, members, props = {}) => ({
  type: 'Feature',
  geometry: { type: 'Point', coordinates: [138.6, -34.9] },
  properties: { id, name: `Suburb ${id}`, postcode: '5000', members, ...props },
});
const ok = (distanceM) => ({ status: 'ok', distanceM });
const assignment = (id, members, route = ok(0), bandId = 'band-0') => ({
  feature: point(id, members), suburbId: id, members, bandId, route,
});
const emptySummary = () => ({
  within: { members: 0, suburbs: 0 }, beyond: { members: 0, suburbs: 0 },
  noRoute: { members: 0, suburbs: 0 }, totalMembers: 0,
});

function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

test('thresholds include zero, exact 50 km, decimal values and the 400 km maximum', () => {
  for (const thresholdKm of [0, 0.5, 25, 50, 400]) {
    const boundaryM = thresholdKm * 1000;
    assert.equal(thresholdStatus(ok(0), thresholdKm), 'within');
    if (boundaryM > 0) assert.equal(thresholdStatus(ok(boundaryM - 0.01), thresholdKm), 'within');
    assert.equal(thresholdStatus(ok(boundaryM), thresholdKm), 'within');
    assert.equal(thresholdStatus(ok(boundaryM + 0.01), thresholdKm), 'beyond');
  }
});

test('invalid thresholds are rejected, including with no assignments or route', () => {
  for (const thresholdKm of [-1, 400.001, NaN, Infinity, -Infinity, undefined, null, '50', '', true, {}, []]) {
    assert.throws(() => thresholdStatus(ok(0), thresholdKm), RangeError);
    assert.throws(() => thresholdStatus(null, thresholdKm), RangeError);
    assert.throws(() => summarizeThreshold([], thresholdKm), RangeError);
    assert.throws(() => prepareMapFeatures([], { thresholdKm }), RangeError);
  }
});

test('minimal successful route metadata suffices without group, geometry or ferry fields', () => {
  assert.equal(thresholdStatus({ status: 'ok', distanceM: 50_000 }, 50), 'within');
  assert.equal(thresholdStatus({ ...ok(50_001), groupId: null, incomplete: true }, 50), 'beyond');
});

test('missing and unsuccessful routes never become within even with a numeric distance', () => {
  for (const route of [undefined, null, {}, { distanceM: 0 }, { status: 'unknown', distanceM: 0 },
    ...['unreachable', 'unsnapped', 'uncomputed'].map((status) => ({ status, distanceM: 0 }))]) {
    assert.equal(thresholdStatus(route, 50), 'no-route');
  }
});

test('malformed road distances are no-route without numeric coercion', () => {
  for (const distanceM of [undefined, null, -0.01, NaN, Infinity, -Infinity, '0', '50000', '', true, false, {}, [], [0]]) {
    assert.equal(thresholdStatus(ok(distanceM), 50), 'no-route');
  }
});

test('empty assignments and zero-member centroids contribute no members or suburbs', () => {
  assert.deepEqual(summarizeThreshold([], 50), emptySummary());
  const zeros = [assignment('a', 0), assignment('b', 0, ok(51_000)), assignment('c', 0, null)];
  assert.deepEqual(summarizeThreshold(zeros, 50), emptySummary());
  assert.deepEqual(prepareMapFeatures(zeros, { thresholdKm: 50 }), { type: 'FeatureCollection', features: [] });
});

test('threshold summaries count members and positive-member suburbs across every route state', () => {
  const assignments = [
    assignment('a', 20), assignment('b', 10, ok(50_000)), assignment('c', 5, ok(50_001)),
    assignment('d', 7, { status: 'unreachable' }), assignment('e', 11, { status: 'unsnapped' }),
    assignment('f', 13, { status: 'uncomputed' }), assignment('g', 17, null),
    assignment('h', 19, ok('0')), assignment('zero', 0),
  ];
  assert.deepEqual(summarizeThreshold(assignments, 50), {
    within: { members: 30, suburbs: 2 }, beyond: { members: 5, suburbs: 1 },
    noRoute: { members: 67, suburbs: 5 }, totalMembers: 102,
  });
});

test('one point with 20 members stays one suburb; repeated IDs and coordinates are not deduplicated', () => {
  const first = assignment('same', 20);
  assert.deepEqual(summarizeThreshold([first], 50).within, { members: 20, suburbs: 1 });
  const assignments = [first, assignment('same', 10)];
  assert.deepEqual(summarizeThreshold(assignments, 50), {
    ...emptySummary(), within: { members: 30, suburbs: 2 }, totalMembers: 30,
  });
  const features = prepareMapFeatures(assignments, { thresholdKm: 50 }).features;
  assert.equal(features.length, 2);
  assert.deepEqual(features.map((feature) => feature.properties.members), [20, 10]);
});

test('malformed member counts follow the existing aggregation policy', () => {
  const assignments = [undefined, null, -1, NaN, Infinity, '20', true].map((members, i) => assignment(`${i}`, members));
  assert.deepEqual(summarizeThreshold(assignments, 50), emptySummary());
  assert.equal(prepareMapFeatures(assignments, { thresholdKm: 50 }).features.length, 0);
});

test('full threshold totals remain independent of visible band and distance filters', () => {
  const features = [point('a', 20), point('b', 10), point('c', 5), point('d', 7), point('z', 0)];
  const analysis = { groups: [{ id: 'group-1' }], distances: {
    a: { 'group-1': ok(0) }, b: { 'group-1': ok(50_000) }, c: { 'group-1': ok(75_000) },
    d: { 'group-1': { status: 'unreachable' } }, z: { 'group-1': ok(0) },
  } };
  const full = freeze(aggregateBandCounts(features, analysis, { groupId: 'group-1' }).assignments);
  const expected = {
    within: { members: 30, suburbs: 2 }, beyond: { members: 5, suburbs: 1 },
    noRoute: { members: 7, suburbs: 1 }, totalMembers: 42,
  };
  for (const options of [{ bandId: 'band-0' }, { minKm: 50, maxKm: 75 }, { bandId: 'unreachable' }, { minKm: 100 }]) {
    const visible = filterAssignments(full, options);
    prepareMapFeatures(visible, { thresholdKm: 50 });
    assert.deepEqual(summarizeThreshold(full, 50), expected);
  }
  assert.equal(summarizeThreshold(full, 75).within.members, 35);
  assert.deepEqual(summarizeThreshold(full, 50), expected);
});

test('threshold helpers consume the route selected by existing group or nearest aggregation', () => {
  const features = [point('a', 20)];
  const analysis = { groups: [{ id: 'group-1' }, { id: 'group-2' }], distances: {
    a: { 'group-1': ok(60_000), 'group-2': ok(50_000) },
  } };
  const selected = aggregateBandCounts(features, analysis, { groupId: 'group-1' }).assignments;
  const nearest = aggregateBandCounts(features, analysis, { mode: 'nearest' }).assignments;
  assert.equal(summarizeThreshold(selected, 50).beyond.members, 20);
  assert.equal(summarizeThreshold(nearest, 50).within.members, 20);
});

test('map features copy only safe map properties and preserve point geometry and member weights', () => {
  const input = assignment('a', 20);
  input.feature.properties = {
    ...input.feature.properties, members: 999, thresholdStatus: 'beyond', bandColor: 'red',
    route: { private: true }, sourceRows: ['private'], other: 'not map data',
  };
  input.feature.geometry.coordinates.push(5);
  freeze(input);
  const output = prepareMapFeatures([input], { thresholdKm: 50 });
  assert.equal(output.type, 'FeatureCollection');
  const [feature] = output.features;
  assert.equal(feature.type, 'Feature');
  assert.deepEqual(feature.properties, {
    id: 'a', name: 'Suburb a', postcode: '5000', members: 20,
    bandColor: '#247e80', thresholdStatus: 'within',
  });
  assert.deepEqual(feature.geometry, input.feature.geometry);
  assert.notStrictEqual(feature, input.feature);
  assert.notStrictEqual(feature.properties, input.feature.properties);
  assert.notStrictEqual(feature.geometry, input.feature.geometry);
  assert.notStrictEqual(feature.geometry.coordinates, input.feature.geometry.coordinates);
  feature.geometry.coordinates[0] = 0;
  feature.properties.members = 1;
  assert.equal(input.feature.geometry.coordinates[0], 138.6);
  assert.equal(input.feature.properties.members, 999);
});

test('map properties have stable defaults when optional feature metadata is absent', () => {
  const input = assignment('fallback-id', 1);
  delete input.feature.properties;
  assert.deepEqual(prepareMapFeatures([input], { thresholdKm: 0 }).features[0].properties, {
    id: 'fallback-id', name: '', postcode: '', members: 1, bandColor: '#247e80', thresholdStatus: 'within',
  });
});

test('map bands use the existing five colours, wrap additional bands and use grey for no-route bands', () => {
  const assignments = Array.from({ length: 6 }, (_, i) => assignment(`${i}`, 1, ok(0), `band-${i}`));
  assignments.push(assignment('unreachable', 1, { status: 'unreachable' }, 'unreachable'));
  assert.deepEqual(prepareMapFeatures(assignments, { thresholdKm: 50 }).features.map((feature) => feature.properties.bandColor),
    ['#247e80', '#58a69a', '#d1ac55', '#c87c4a', '#a95253', '#247e80', '#87949b']);
});

test('map features accept a custom palette and classify every positive point at the supplied threshold', () => {
  const assignments = freeze([
    assignment('a', 20, ok(50_000), 'band-0'), assignment('b', 10, ok(50_001), 'band-1'),
    assignment('c', 7, { status: 'unsnapped' }, 'unsnapped'), assignment('z', 0),
  ]);
  const palette = freeze(['#112233', '#445566']);
  const features = prepareMapFeatures(assignments, { thresholdKm: 50, bandColors: palette }).features;
  assert.deepEqual(features.map((feature) => feature.properties.thresholdStatus), ['within', 'beyond', 'no-route']);
  assert.deepEqual(features.map((feature) => feature.properties.bandColor), ['#112233', '#445566', '#87949b']);
  assert.equal(assignments.length, 4);
});

test('cluster properties sum member weights and partition them by exact threshold status', () => {
  assert.deepEqual(clusterProperties, {
    memberTotal: ['+', ['get', 'members']],
    withinMembers: ['+', ['case', ['==', ['get', 'thresholdStatus'], 'within'], ['get', 'members'], 0]],
    beyondMembers: ['+', ['case', ['==', ['get', 'thresholdStatus'], 'beyond'], ['get', 'members'], 0]],
    noRouteMembers: ['+', ['case', ['==', ['get', 'thresholdStatus'], 'no-route'], ['get', 'members'], 0]],
  });
});

function evaluate(expression, properties) {
  if (!Array.isArray(expression)) return expression;
  const [operator, ...args] = expression;
  switch (operator) {
    case 'get': return properties[args[0]];
    case '==': return evaluate(args[0], properties) === evaluate(args[1], properties);
    case 'case': return evaluate(args[0], properties) ? evaluate(args[1], properties) : evaluate(args[2], properties);
    default: throw new Error(`Unexpected expression: ${operator}`);
  }
}

function cluster(features) {
  const properties = { point_count: features.length };
  for (const [name, [reduce, expression]] of Object.entries(clusterProperties)) {
    assert.equal(reduce, '+');
    properties[name] = features.reduce((sum, feature) => sum + evaluate(expression, feature.properties), 0);
  }
  return clusterSummary(properties);
}

test('one 20-member point and two 30-member points report members separately from point_count', () => {
  const features = prepareMapFeatures([assignment('a', 20), assignment('b', 10, ok(50_001))], { thresholdKm: 50 }).features;
  assert.deepEqual(cluster(features.slice(0, 1)), {
    members: 20, suburbs: 1, withinMembers: 20, beyondMembers: 0, noRouteMembers: 0,
  });
  assert.deepEqual(cluster(features), {
    members: 30, suburbs: 2, withinMembers: 20, beyondMembers: 10, noRouteMembers: 0,
  });
});

test('evaluated cluster member partitions reconcile with full threshold statistics', () => {
  const assignments = [assignment('a', 20), assignment('b', 10, ok(50_000)), assignment('c', 5, ok(51_000)),
    assignment('d', 7, null, 'uncomputed'), assignment('z', 0)];
  for (const thresholdKm of [0, 50, 400]) {
    const totals = summarizeThreshold(assignments, thresholdKm);
    const summary = cluster(prepareMapFeatures(assignments, { thresholdKm }).features);
    assert.equal(summary.members, totals.totalMembers);
    assert.equal(summary.suburbs, 4);
    assert.equal(summary.withinMembers, totals.within.members);
    assert.equal(summary.beyondMembers, totals.beyond.members);
    assert.equal(summary.noRouteMembers, totals.noRoute.members);
    assert.equal(summary.withinMembers + summary.beyondMembers + summary.noRouteMembers, summary.members);
  }
});

test('cluster summaries read computed totals and default missing or malformed properties to zero', () => {
  const zero = { members: 0, suburbs: 0, withinMembers: 0, beyondMembers: 0, noRouteMembers: 0 };
  for (const properties of [undefined, null, {}, { members: 30, cluster_id: 123 }]) {
    assert.deepEqual(clusterSummary(properties), zero);
  }
  assert.deepEqual(clusterSummary({ point_count: 2, memberTotal: 30 }), { ...zero, members: 30, suburbs: 2 });
  for (const value of [undefined, null, -1, NaN, Infinity, '30', true, {}]) {
    assert.deepEqual(clusterSummary({ memberTotal: value, point_count: value, withinMembers: value, beyondMembers: value, noRouteMembers: value }), zero);
  }
});
