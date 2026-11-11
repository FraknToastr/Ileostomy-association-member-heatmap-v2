import test from 'node:test';
import assert from 'node:assert/strict';
import { createExpression, validateStyleMin } from '@maplibre/maplibre-gl-style-spec';
import { initialStyle, addAnalysisLayers, applyAppearance, BASEMAPS, FONT_STACK, PALETTES } from '../src/map-style.js';

const settings = { basemap: 'osm', palette: 'blue', showMembers: true, showRoute: true,
  suburbLabels: false, clusterLabels: true, mapMode: 'points' };
const modes = ['points', 'clusters', 'density', 'bands'];

function analysisMap() {
  const style = initialStyle('https://example.org/heatmap/index.html');
  const layer = id => {
    const result = style.layers.find(layer => layer.id === id);
    assert.ok(result, `Layer ${id} exists`);
    return result;
  };
  const map = {
    addSource: (id, source) => { style.sources[id] = source; },
    addLayer: layer => style.layers.push(layer),
    setLayoutProperty: (id, key, value) => { (layer(id).layout ??= {})[key] = value; },
    setPaintProperty: (id, key, value) => { layer(id).paint[key] = value; },
  };
  addAnalysisLayers(map);
  return { style, map, layer };
}

function evaluate(value, properties) {
  if (!Array.isArray(value)) return value;
  const expression = createExpression(value);
  assert.equal(expression.result, 'success');
  return expression.value.evaluate({}, { type: 1, properties });
}

function assertPointPaint(layer) {
  assert.equal(layer.paint['circle-opacity'], 1);
  assert.equal(layer.paint['circle-stroke-color'], '#ffffff');
  assert.equal(layer.paint['circle-stroke-width'], 2.5);
  for (const [members, radius] of [[1, 6], [10, 8], [50, 12], [150, 16], [30, 10]]) {
    assert.equal(evaluate(layer.paint['circle-radius'], { members }), radius);
  }
}

test('initial and analytical styles use hosted glyphs without repository PBF files', () => {
  const style = initialStyle('https://example.org/heatmap/index.html');
  assert.equal(style.glyphs, 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf');
  assert.deepEqual(validateStyleMin(style).map(e => e.message), []);
  addAnalysisLayers({ addSource: (id, source) => { style.sources[id] = source; }, addLayer: layer => style.layers.push(layer) });
  assert.deepEqual(validateStyleMin(style).map(e => e.message), []);
  assert.equal(style.sources['member-clusters'].cluster, true);
  assert.deepEqual(style.layers.find(l => l.id === 'cluster-count-labels').layout['text-font'], [FONT_STACK]);
});

test('basemaps use provider-specific tile order and only OSM starts visible', () => {
  const style = initialStyle('https://example.org/');
  assert.match(BASEMAPS.osm.tiles[0], /\{z\}\/\{x\}\/\{y\}/);
  for (const id of ['topo', 'aerial']) assert.match(BASEMAPS[id].tiles[0], /tile\/\{z\}\/\{y\}\/\{x\}/);
  assert.deepEqual(style.layers.filter(l => l.type === 'raster' && l.layout.visibility === 'visible').map(l => l.id), ['basemap-osm']);
});

test('approved vivid palettes replace teal and purple with blue as the default', () => {
  assert.deepEqual(Object.keys(PALETTES), ['blue', 'magenta', 'violet', 'orange']);
  for (const [key, label, point] of [
    ['blue', 'Electric Blue', '#006BFF'], ['magenta', 'Magenta', '#F00078'],
    ['violet', 'Vivid Violet', '#7A24FF'], ['orange', 'Bright Orange', '#FF6500'],
  ]) {
    assert.equal(PALETTES[key].label, label);
    assert.equal(PALETTES[key].point, point);
    assert.match(PALETTES[key].light, /^#[0-9a-f]{6}$/i);
    assert.match(PALETTES[key].dark, /^#[0-9a-f]{6}$/i);
  }
  const { map, layer } = analysisMap();
  for (const id of ['member-points', 'cluster-points']) {
    assert.equal(layer(id).paint['circle-color'], '#006BFF');
    assertPointPaint(layer(id));
  }
  for (const palette of [undefined, 'teal']) {
    applyAppearance(map, { ...settings, palette });
    assert.equal(layer('member-points').paint['circle-color'], '#006BFF');
  }
});

test('points and unclustered members use vivid fills, full opacity and white halos', () => {
  const { map, layer } = analysisMap();
  for (const [palette, colors] of Object.entries(PALETTES)) {
    for (const mapMode of ['points', 'clusters']) {
      applyAppearance(map, { ...settings, palette, mapMode });
      for (const id of ['member-points', 'cluster-points']) {
        assert.equal(layer(id).paint['circle-color'], colors.point);
        assertPointPaint(layer(id));
      }
      const clusterColor = layer('member-cluster-circles').paint['circle-color'];
      assert.deepEqual(clusterColor, ['interpolate', ['linear'], ['get', 'memberTotal'],
        2, colors.light, 100, colors.point, 1000, colors.dark]);
    }
  }
});

test('threshold rings sit behind their points and keep threshold color separate from white halos', () => {
  const { style, layer } = analysisMap();
  for (const [ringId, pointId, source] of [
    ['member-threshold-rings', 'member-points', 'members'],
    ['cluster-threshold-rings', 'cluster-points', 'member-clusters'],
  ]) {
    const ring = layer(ringId);
    const point = layer(pointId);
    assert.equal(ring.type, 'circle');
    assert.equal(ring.source, source);
    assert.ok(style.layers.indexOf(ring) < style.layers.indexOf(point));
    assert.equal(ring.paint['circle-color'], 'rgba(0,0,0,0)');
    assert.equal(ring.paint['circle-stroke-width'], 2);
    assertPointPaint(point);
    for (const thresholdStatus of ['beyond', 'no-route', 'within', 'no-target', undefined]) {
      assert.equal(evaluate(ring.filter, { thresholdStatus }), ['beyond', 'no-route'].includes(thresholdStatus));
    }
    for (const members of [1, 10, 50, 150]) {
      assert.equal(evaluate(ring.paint['circle-radius'], { members }),
        evaluate(point.paint['circle-radius'], { members }) + 4);
    }
    assert.equal(evaluate(ring.paint['circle-stroke-color'], { thresholdStatus: 'beyond' }), '#FF6500');
    assert.equal(evaluate(ring.paint['circle-stroke-color'], { thresholdStatus: 'no-route' }), '#6e7780');
  }
  for (const thresholdStatus of ['beyond', 'no-route']) {
    assert.equal(evaluate(layer('cluster-threshold-rings').filter, { thresholdStatus, point_count: 2 }), false);
  }
});

test('threshold-ring visibility follows its corresponding point layer in every mode', () => {
  const { map, layer } = analysisMap();
  for (const showMembers of [true, false]) {
    for (const mapMode of modes) {
      applyAppearance(map, { ...settings, mapMode, showMembers });
      for (const [ringId, pointId, clustered] of [
        ['member-threshold-rings', 'member-points', false],
        ['cluster-threshold-rings', 'cluster-points', true],
      ]) {
        const visibility = showMembers && (mapMode === 'clusters') === clustered ? 'visible' : 'none';
        assert.equal(layer(pointId).layout.visibility, visibility);
        assert.equal(layer(ringId).layout.visibility, visibility);
      }
      assert.equal(layer('member-heat').layout.visibility, showMembers && mapMode === 'density' ? 'visible' : 'none');
    }
  }
});

test('density keeps its heatmap ramp and subdued teal point overlay across palettes', () => {
  const { map, layer } = analysisMap();
  const heatmapRamp = ['interpolate', ['linear'], ['heatmap-density'],
    0, 'rgba(36,126,128,0)', .15, '#dcecc7', .4, '#88cbbb', .7, '#279da5', 1, '#194674'];
  for (const palette of Object.keys(PALETTES)) {
    applyAppearance(map, { ...settings, palette, mapMode: 'density' });
    assert.equal(layer('member-points').paint['circle-color'], '#247e80');
    assert.equal(layer('member-points').paint['circle-opacity'], .45);
    assert.equal(layer('member-points').paint['circle-stroke-color'], '#ffffff');
    assert.equal(layer('member-points').paint['circle-stroke-width'], 2.5);
    assert.deepEqual(layer('member-heat').paint['heatmap-color'], heatmapRamp);
    for (const [members, radius] of [[1, 3], [10, 5], [50, 10], [150, 14]]) {
      assert.equal(evaluate(layer('member-points').paint['circle-radius'], { members }), radius);
      assert.equal(evaluate(layer('member-threshold-rings').paint['circle-radius'], { members }), radius + 4);
    }
  }
});

test('distance bands retain semantic fills and restore larger opaque points after density', () => {
  const { map, layer } = analysisMap();
  for (const palette of Object.keys(PALETTES)) {
    applyAppearance(map, { ...settings, palette, mapMode: 'density' });
    for (const mapMode of ['bands', 'points']) {
      applyAppearance(map, { ...settings, palette, mapMode });
      assertPointPaint(layer('member-points'));
      if (mapMode === 'bands') {
        assert.deepEqual(layer('member-points').paint['circle-color'], ['get', 'bandColor']);
        assert.equal(evaluate(layer('member-points').paint['circle-color'], { bandColor: '#123456' }), '#123456');
      }
      for (const members of [1, 10, 50, 150]) {
        assert.equal(evaluate(layer('member-threshold-rings').paint['circle-radius'], { members }),
          evaluate(layer('member-points').paint['circle-radius'], { members }) + 4);
      }
    }
  }
});

test('cluster circles and labels continue counting members rather than suburb centroids', () => {
  const { style, map, layer } = analysisMap();
  assert.deepEqual(style.sources['member-clusters'].clusterProperties.memberTotal, ['+', ['get', 'members']]);
  applyAppearance(map, { ...settings, mapMode: 'clusters' });
  const properties = { memberTotal: 100, point_count: 2 };
  assert.equal(evaluate(layer('member-cluster-circles').paint['circle-radius'], properties), 23);
  assert.equal(evaluate(layer('cluster-count-labels').layout['text-field'], properties), '100');
  assert.deepEqual(layer('member-cluster-circles').filter, ['has', 'point_count']);
  assert.deepEqual(layer('cluster-points').filter, ['!', ['has', 'point_count']]);
});

test('every palette and appearance mode produces a valid MapLibre style', () => {
  const { style, map } = analysisMap();
  for (const palette of Object.keys(PALETTES)) {
    for (const mapMode of modes) {
      for (const showMembers of [true, false]) {
        applyAppearance(map, { ...settings, palette, mapMode, showMembers });
        assert.deepEqual(validateStyleMin(style).map(e => e.message), [], `${palette}/${mapMode}/${showMembers}`);
      }
    }
  }
});
