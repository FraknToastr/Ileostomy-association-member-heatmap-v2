import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';

const dataRoot = new URL('../public/data/', import.meta.url);
const sourceAnalysis = JSON.parse(await fs.readFile(new URL('analysis.json', dataRoot), 'utf8'));
const members = JSON.parse(await fs.readFile(new URL('members.geojson', dataRoot), 'utf8')).features;
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');

test.beforeEach(async ({ page }) => {
  await page.route('https://demotiles.maplibre.org/font/**', async route => {
    const url = new URL(route.request().url());
    const name = url.pathname.split('/').pop();
    if (!['0-255.pbf', '256-511.pbf', '8192-8447.pbf'].includes(name)) return route.abort();
    const body = await fs.readFile(new URL(`../.generated/test-glyphs/Noto Sans Regular/${name}`, import.meta.url));
    return route.fulfill({ contentType: 'application/x-protobuf', headers: { 'access-control-allow-origin': '*' }, body });
  });
  await page.route(/https:\/\/(tile\.openstreetmap\.org|services\.arcgisonline\.com)\/.+/, route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: png }));
  await page.addInitScript(() => {
    let library;
    window.__mapErrors = [];
    window.__popupConstructions = 0;
    Object.defineProperty(window, 'maplibregl', {
      configurable: true, get: () => library,
      set(value) {
        library = value;
        queueMicrotask(() => {
          value.Popup = new Proxy(value.Popup, {
            construct(Constructor, args) {
              window.__popupConstructions++;
              return Reflect.construct(Constructor, args);
            },
          });
          value.Map = new Proxy(value.Map, {
            construct(Constructor, args) {
              const map = Reflect.construct(Constructor, args);
              window.__markerTestMap = map;
              map.on('error', e => window.__mapErrors.push({ source: e.sourceId, message: e.error?.message }));
              return map;
            },
          });
        });
      },
    });
  });
});

test.afterEach(async ({ page }) => {
  const errors = await page.evaluate(() => (window.__mapErrors || []).filter(e => !e.source?.startsWith('basemap-')));
  expect(errors, 'No analytical layer, font, or expression errors').toEqual([]);
  await expect(page.locator('.maplibregl-popup')).toHaveCount(0);
  expect(await page.evaluate(() => window.__popupConstructions ?? 0), 'No MapLibre popups are constructed').toBe(0);
});

async function ready(page) {
  await page.goto('/');
  await expect(page.locator('#joined-total')).toHaveText('3,019');
  await expect(page.locator('.group-marker')).toHaveCount(11);
  await expect(page.locator('#data-notice')).toBeHidden();
}
async function openMap(page) {
  if (!await page.locator('#map-controls-panel').evaluate(el => el.matches(':popover-open'))) {
    await page.locator('#map-controls-toggle').click();
  }
  await expect(page.locator('#map-controls-panel')).toBeVisible();
  await expect(page.locator('#display-filters-panel')).toBeHidden();
}
async function closeMap(page) {
  if (await page.locator('#map-controls-panel').evaluate(el => el.matches(':popover-open'))) {
    await page.locator('#map-controls-close').click();
  }
  await expect(page.locator('#map-controls-panel')).toBeHidden();
}
async function openFilters(page) {
  if (!await page.locator('#display-filters-panel').evaluate(el => el.matches(':popover-open'))) {
    await page.locator('#display-filters-toggle').click();
  }
  await expect(page.locator('#display-filters-panel')).toBeVisible();
  await expect(page.locator('#map-controls-panel')).toBeHidden();
}
async function mode(page, value) {
  await openMap(page);
  await page.locator(`input[name="map-mode"][value="${value}"]`).check();
}
async function threshold(page, km) {
  await page.locator('#support-distance').evaluate((el, value) => {
    el.value = String(value); el.dispatchEvent(new Event('input', { bubbles: true }));
  }, km);
  await expect(page.locator('#support-distance-value')).toHaveText(`${km} km`);
}
function totals(analysis, km, groupId = null) {
  const sums = { within: 0, beyond: 0, noRoute: 0 };
  for (const f of members) {
    const entries = groupId ? [analysis.distances[f.properties.id][groupId]] : Object.values(analysis.distances[f.properties.id]);
    const distances = entries.filter(e => e.status === 'ok' && Number.isFinite(e.distanceM)).map(e => e.distanceM);
    const category = !distances.length ? 'noRoute' : Math.min(...distances) <= km * 1000 ? 'within' : 'beyond';
    sums[category] += f.properties.members;
  }
  return sums;
}
async function expectTotals(page, expected) {
  for (const [id, key] of [['threshold-within', 'within'], ['threshold-beyond', 'beyond'], ['threshold-no-route', 'noRoute']]) {
    await expect(page.locator(`#${id}`)).toHaveText(expected[key].toLocaleString('en-AU'));
  }
  expect(expected.within + expected.beyond + expected.noRoute).toBe(3019);
  await expect(page.locator('#table-member-total')).toHaveText('3,019');
}

async function routeFixture(name) {
  const feature = members.find(member => member.properties.name === name);
  if (!feature) throw new Error(`Missing suburb fixture: ${name}`);
  const [route] = sourceAnalysis.groups.map(group => ({
    ...sourceAnalysis.distances[feature.properties.id][group.id], groupId: group.id,
  })).filter(entry => entry.status === 'ok').sort((a, b) => a.distanceM - b.distanceM);
  if (!route) throw new Error(`Missing reachable route fixture: ${name}`);
  const collection = JSON.parse(await fs.readFile(new URL(`routes/${route.groupId}.geojson`, dataRoot), 'utf8'));
  const line = collection.features.find(item => item.properties.suburbId === feature.properties.id);
  if (!line) throw new Error(`Missing precomputed geometry: ${name}`);
  return { feature, route, line, collection, group: sourceAnalysis.groups.find(group => group.id === route.groupId) };
}

async function searchSelect(page, feature) {
  await page.locator('#suburb-search').fill(feature.properties.name);
  await page.locator('#search-results').selectOption(feature.properties.id);
  await expect(page.locator('#selected-name')).toHaveText(feature.properties.name);
}

async function cameraView(page) {
  return page.evaluate(() => {
    const map = window.__markerTestMap;
    const round = value => Number(value.toFixed(6));
    return { center: map.getCenter().toArray().map(round), zoom: round(map.getZoom()),
      bearing: round(map.getBearing()), pitch: round(map.getPitch()),
      padding: Object.fromEntries(Object.entries(map.getPadding()).map(([key, value]) => [key, round(value)])) };
  });
}

async function expectCameraView(page, expected) {
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.isMoving())).toBe(false);
  await expect.poll(() => cameraView(page)).toEqual(expected);
}

async function captureFits(page) {
  await page.evaluate(() => {
    const map = window.__markerTestMap;
    const original = map.fitBounds.bind(map);
    window.__routeFitCalls = [];
    map.fitBounds = (bounds, options) => {
      window.__routeFitCalls.push({ maxZoom: options?.maxZoom });
      return original(bounds, options);
    };
  });
}

async function expectReadyRoute(page, fixture) {
  await expect.poll(() => page.evaluate(() =>
    window.__markerTestMap.getStyle().sources.route.data.features.map(feature => feature.properties.suburbId)))
    .toEqual([fixture.feature.properties.id]);
  await expect(page.locator('#route-status')).toContainText('Road route ready');
  await expect(page.locator('#route-status')).toContainText(fixture.group.name);
  await expect(page.locator('#fit-route')).toBeEnabled();
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.isMoving())).toBe(false);
  const line = await page.evaluate(() => window.__markerTestMap.getStyle().sources.route.data.features[0]);
  expect(line.geometry, 'The displayed line uses the entire precomputed geometry').toEqual(fixture.line.geometry);
}

async function expectRouteFits(page) {
  await expect.poll(() => page.evaluate(() => {
    const map = window.__markerTestMap;
    const geometry = map.getStyle().sources.route.data.features[0]?.geometry;
    if (!geometry) return false;
    const coordinates = geometry.type === 'MultiLineString' ? geometry.coordinates.flat() : geometry.coordinates;
    const points = coordinates.map(coordinate => map.project(coordinate));
    const left = Math.min(...points.map(point => point.x));
    const right = Math.max(...points.map(point => point.x));
    const top = Math.min(...points.map(point => point.y));
    const bottom = Math.max(...points.map(point => point.y));
    const { width, height } = map.getContainer().getBoundingClientRect();
    const padding = map.getPadding();
    const usableWidth = Math.max(1, width - padding.left - padding.right);
    const usableHeight = Math.max(1, height - padding.top - padding.bottom);
    const fill = Math.max((right - left) / usableWidth, (bottom - top) / usableHeight);
    return !map.isMoving() && coordinates.length > 1 && left >= -1 && top >= -1
      && right <= width + 1 && bottom <= height + 1 && fill > 0.3 && map.getZoom() <= 17.001;
  }), { message: 'Every route coordinate, including both endpoints, fits the map with a useful scale' }).toBe(true);
}

test('header Map opens a native dialog popover, manages focus, and fits mobile', async ({ page }) => {
  await ready(page);
  await expect(page.locator('header #map-controls-toggle')).toHaveText('Map');
  const panel = page.locator('#map-controls-panel');
  expect(await panel.evaluate(el => el.tagName)).toBe('DIV');
  await expect(panel).toHaveAttribute('popover', 'auto');
  await expect(panel).toHaveAttribute('role', 'dialog');
  await expect(panel).toBeHidden();
  await expect(page.locator('#map-controls-toggle')).toHaveAttribute('aria-expanded', 'false');
  await openMap(page);
  await expect(page.locator('#map-controls-heading')).toBeFocused();
  await expect(page.locator('#map-controls-toggle')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#basemap-select')).toHaveValue('osm');
  await expect(page.locator('#symbol-palette')).toHaveValue('blue');
  await expect(page.locator('#suburb-label-toggle')).not.toBeChecked();
  await expect(page.locator('#cluster-label-toggle')).toBeChecked();
  await expect(page.locator('#group-label-toggle')).toBeChecked();
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(page.locator('#map-controls-toggle')).toBeFocused();
  await expect(page.locator('#map-controls-toggle')).toHaveAttribute('aria-expanded', 'false');
  await page.setViewportSize({ width: 390, height: 844 });
  await openMap(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.locator('#map-controls-close').click();
  await expect(page.locator('#map-controls-toggle')).toBeFocused();
});

test('header menus are exclusive and native light dismissal returns focus', async ({ page }) => {
  await ready(page);
  const filters = page.locator('#display-filters-panel');
  await expect(page.locator('header #display-filters-toggle')).toHaveText('Display Filters');
  await expect(filters).toHaveAttribute('popover', 'auto');
  expect(await page.locator('header').evaluate(el => {
    const buttons = [...el.querySelectorAll('button')].map(button => button.id);
    return buttons.indexOf('theme-toggle') === buttons.indexOf('display-filters-toggle') + 1;
  }), 'Display Filters sits next to the theme button').toBe(true);
  await openMap(page);
  await openFilters(page);
  for (const id of ['clear-filters', 'range-form', 'distance-min', 'distance-max', 'visible-members', 'active-band']) {
    await expect(filters.locator(`#${id}`)).toBeVisible();
  }
  await openMap(page);
  await expect(page.locator('#map-controls-heading')).toBeFocused();
  // The header title is outside either menu and cannot steal focus itself.
  await page.locator('header h1').click();
  await expect(page.locator('#map-controls-panel')).toBeHidden();
  await expect(page.locator('#map-controls-toggle')).toBeFocused();
  await openFilters(page);
  await page.locator('#distance-min').focus();
  await page.keyboard.press('Escape');
  await expect(filters).toBeHidden();
  await expect(page.locator('#display-filters-toggle')).toBeFocused();
  await openFilters(page);
  await page.locator('#display-filters-close').click();
  await expect(filters).toBeHidden();
  await expect(page.locator('#display-filters-toggle')).toBeFocused();
  await openFilters(page);
  await page.locator('header h1').click();
  await expect(filters).toBeHidden();
  await expect(page.locator('#display-filters-toggle')).toBeFocused();
});

test('basemaps preserve camera and selected route; attribution lives in a collapsed info control', async ({ page }) => {
  await ready(page);
  const attribution = page.locator('.maplibregl-ctrl-attrib');
  await expect(attribution).not.toHaveAttribute('open');
  await expect(attribution).not.toHaveClass(/maplibregl-compact-show/);
  await page.locator('#suburb-search').fill('ADELAIDE');
  await page.locator('#search-results').selectOption({ label: await page.locator('#search-results option').first().textContent() });
  await expect(page.locator('#route-status')).toContainText('Road route ready');
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.isMoving())).toBe(false);
  const before = await page.evaluate(() => {
    const map = window.__markerTestMap;
    return { center: map.getCenter().toArray(), zoom: map.getZoom(), route: map.getStyle().sources.route.data };
  });
  for (const id of ['topo', 'aerial', 'osm']) {
    await openMap(page);
    await page.locator('#basemap-select').selectOption(id);
    await expect.poll(() => page.evaluate(key => window.__markerTestMap.getLayoutProperty(`basemap-${key}`, 'visibility'), id)).toBe('visible');
    const after = await page.evaluate(() => {
      const map = window.__markerTestMap;
      return { center: map.getCenter().toArray(), zoom: map.getZoom(), route: map.getStyle().sources.route.data };
    });
    expect(after).toEqual(before);
    await expect(page.locator('#selected-suburb')).toBeVisible();
    await expect(page.locator('#table-member-total')).toHaveText('3,019');
    await expect.poll(() => attribution.textContent()).toContain(id === 'topo' ? 'Esri World Topography' : id === 'aerial' ? 'Esri World Imagery' : 'OpenStreetMap');
    if (id !== 'topo') await expect.poll(() => attribution.textContent()).not.toContain('Esri World Topography');
    if (id !== 'aerial') await expect.poll(() => attribution.textContent()).not.toContain('Esri World Imagery');
    await expect(attribution).not.toHaveAttribute('open');
    await page.locator('.maplibregl-ctrl-attrib-button').click();
    const fits = await attribution.evaluate(el => {
      const credits = el.getBoundingClientRect();
      const map = document.getElementById('map').getBoundingClientRect();
      return credits.left >= map.left && credits.right <= map.right;
    });
    expect(fits, 'Full provider credits fit within the map').toBe(true);
    await page.locator('.maplibregl-ctrl-attrib-button').click();
  }
  await page.locator('.maplibregl-ctrl-attrib-button').click();
  await expect(page.locator('.maplibregl-ctrl-attrib-inner')).toBeVisible();
  await expect(attribution).toContainText('Road data');
  await expect(attribution).toContainText('MapLibre');
  await page.locator('.maplibregl-ctrl-attrib-button').click();
  await expect(page.locator('.maplibregl-ctrl-attrib-inner')).toBeHidden();
});

test('point and cluster palettes and label toggles work with hosted glyphs', async ({ page }) => {
  const glyphResponses = [];
  page.on('response', response => { if (response.url().startsWith('https://demotiles.maplibre.org/font/')) glyphResponses.push(response.status()); });
  await ready(page);
  await mode(page, 'points');
  expect(await page.evaluate(() => window.__markerTestMap.getPaintProperty('member-points', 'circle-color'))).toBe('#006BFF');
  const colors = { blue: '#006BFF', magenta: '#F00078', violet: '#7A24FF', orange: '#FF6500' };
  for (const [id, color] of Object.entries(colors)) {
    await page.locator('#symbol-palette').selectOption(id);
    expect(await page.evaluate(() => window.__markerTestMap.getPaintProperty('member-points', 'circle-color'))).toBe(color);
  }
  await page.locator('#suburb-label-toggle').check();
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [138.6, -34.93], zoom: 10 }));
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.queryRenderedFeatures({ layers: ['suburb-labels'] }).length)).toBeGreaterThan(0);
  expect(glyphResponses).toContain(200);
  await mode(page, 'clusters');
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [138.6, -34.93], zoom: 7 }));
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.queryRenderedFeatures({ layers: ['cluster-count-labels'] }).length)).toBeGreaterThan(0);
  await page.locator('#cluster-label-toggle').uncheck();
  expect(await page.evaluate(() => window.__markerTestMap.getLayoutProperty('cluster-count-labels', 'visibility'))).toBe('none');
  expect(await page.evaluate(() => window.__markerTestMap.getLayoutProperty('cluster-suburb-labels', 'visibility'))).toBe('visible');
  await page.locator('#suburb-label-toggle').uncheck();
  await page.locator('#cluster-label-toggle').check();
  expect(await page.evaluate(() => window.__markerTestMap.getLayoutProperty('cluster-count-labels', 'visibility'))).toBe('visible');
  expect(await page.evaluate(() => window.__markerTestMap.getLayoutProperty('cluster-suburb-labels', 'visibility'))).toBe('none');
  expect(await page.evaluate(() => window.__markerTestMap.getPaintProperty('cluster-points', 'circle-color'))).toBe('#FF6500');
});

test('support-group labels toggle independently of support-group markers', async ({ page }) => {
  await ready(page);
  await openMap(page);
  await expect(page.locator('#group-label-toggle')).toBeChecked();
  await page.locator('#group-label-toggle').uncheck();
  await expect(page.locator('#group-layer-toggle')).toBeChecked();
  await expect(page.locator('.group-name').first()).toBeHidden();
  await expect(page.locator('.group-marker').first()).toBeVisible();
  await page.locator('#group-layer-toggle').uncheck();
  await page.locator('#group-label-toggle').check();
  await expect(page.locator('#group-layer-toggle')).not.toBeChecked();
  await expect(page.locator('.group-marker').first()).toBeHidden();
  await page.locator('#group-layer-toggle').check();
  await expect(page.locator('.group-marker').first()).toBeVisible();
  await expect(page.locator('.group-name').first()).toBeVisible();
  await expect(page.locator('#table-member-total')).toHaveText('3,019');
});

test('clusters sum member leaves and threshold counts, then zoom without a popup', async ({ page }) => {
  await ready(page);
  await mode(page, 'clusters');
  await page.locator('#group-layer-toggle').uncheck();
  await closeMap(page);
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [138.6, -34.93], zoom: 7 }));
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.queryRenderedFeatures({ layers: ['member-cluster-circles'] }).length)).toBeGreaterThan(0);
  const cluster = await page.evaluate(async () => {
    const map = window.__markerTestMap;
    const features = map.queryRenderedFeatures({ layers: ['member-cluster-circles'] });
    const selected = features.sort((a, b) => b.properties.memberTotal - a.properties.memberTotal)[0];
    const leaves = await map.getSource('member-clusters').getClusterLeaves(selected.properties.cluster_id, selected.properties.point_count, 0);
    const sums = { members: 0, within: 0, beyond: 0, noRoute: 0 };
    for (const leaf of leaves) {
      sums.members += leaf.properties.members;
      sums[leaf.properties.thresholdStatus === 'no-route' ? 'noRoute' : leaf.properties.thresholdStatus] += leaf.properties.members;
    }
    const point = map.project(selected.geometry.coordinates);
    const bounds = map.getContainer().getBoundingClientRect();
    return { properties: selected.properties, sums, leaves: leaves.length, point: [bounds.left + point.x, bounds.top + point.y], zoom: map.getZoom() };
  });
  expect(cluster.properties.memberTotal).toBe(cluster.sums.members);
  expect(cluster.properties.point_count).toBe(cluster.leaves);
  expect(cluster.sums.members).toBeGreaterThan(cluster.leaves);
  expect(cluster.properties.withinMembers).toBe(cluster.sums.within);
  expect(cluster.properties.beyondMembers).toBe(cluster.sums.beyond);
  expect(cluster.properties.noRouteMembers).toBe(cluster.sums.noRoute);
  await page.mouse.click(...cluster.point);
  await expect.poll(() => page.evaluate(() => window.__markerTestMap.getZoom())).toBeGreaterThan(cluster.zoom);
  await expect(page.locator('.maplibregl-popup')).toHaveCount(0);
});

test('slider uses inclusive road distances and all-member totals in nearest and selected-group modes', async ({ page }) => {
  const analysis = structuredClone(sourceAnalysis);
  const fixtures = members.filter(f => f.properties.members > 0).slice(0, 3);
  for (const [index, f] of fixtures.entries()) for (const group of analysis.groups) {
    analysis.distances[f.properties.id][group.id] = { status: index === 2 ? 'unsnapped' : 'ok', distanceM: index === 0 ? 50000 : index === 1 ? 50001 : null, usesFerry: false };
  }
  await page.route('**/public/data/analysis.json', route => route.fulfill({ json: analysis }));
  await ready(page);
  await expect(page.locator('#support-distance')).toHaveAttribute('min', '0');
  await expect(page.locator('#support-distance')).toHaveAttribute('max', '400');
  await expect(page.locator('#support-distance')).toHaveAttribute('step', '10');
  await expect(page.locator('#support-distance-value')).toHaveText('50 km');
  await expect(page.locator('#support-distance-target')).toContainText(/nearest/i);
  const sliderName = await page.locator('#support-distance').getAttribute('aria-label');
  expect(sliderName, 'The slider has a stable accessible name').toBeTruthy();
  await expectTotals(page, totals(analysis, 50));
  for (const [f, label] of [[fixtures[0], 'Within'], [fixtures[1], 'Beyond'], [fixtures[2], 'No computed']]) {
    await page.locator('#suburb-search').fill(f.properties.name);
    await page.locator('#search-results').selectOption(f.properties.id);
    await expect(page.locator('#selected-threshold')).toContainText(label);
  }
  await page.locator('#close-selection').click();
  for (const km of [0, 10, 400, 50]) { await threshold(page, km); await expectTotals(page, totals(analysis, km)); }
  await page.locator('#support-distance').focus();
  await page.keyboard.press('Home');
  await expect(page.locator('#support-distance-value')).toHaveText('0 km');
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#support-distance-value')).toHaveText('10 km');
  await page.locator('input[name="analysis-mode"][value="group"]').check();
  await page.locator('#group-select').selectOption('group-1');
  await expect(page.locator('#support-distance-target')).toContainText(sourceAnalysis.groups.find(group => group.id === 'group-1').name);
  await expect(page.locator('#support-distance')).toHaveAttribute('aria-label', sliderName);
  await expectTotals(page, totals(analysis, 10, 'group-1'));
  await page.locator('#band-rows .band-button').first().click();
  await expectTotals(page, totals(analysis, 10, 'group-1'));
});

test('failed imagery tiles leave analysis available and another basemap recovers', async ({ page }) => {
  await page.route('**/World_Imagery/MapServer/tile/**', route => route.abort());
  await ready(page);
  await openMap(page);
  await page.locator('#basemap-select').selectOption('aerial');
  await expect(page.locator('#map-message')).toContainText('Esri Aerial tiles are unavailable');
  await expect(page.locator('#joined-total')).toHaveText('3,019');
  await page.locator('#basemap-select').selectOption('osm');
  await expect(page.locator('#map-notice')).toBeHidden();
  await expect(page.locator('#export-csv')).toBeEnabled();
});

test('search selection fits the complete route and Fit Route restores it after a manual pan', async ({ page }) => {
  const fixture = await routeFixture('CEDUNA');
  expect(fixture.line.geometry.coordinates.length, 'Use a route with substantial intermediate geometry').toBeGreaterThan(20);
  const snapRequests = [];
  page.on('request', request => { if (request.url().includes('snap-report')) snapRequests.push(request.url()); });
  await ready(page);
  await expect(page.locator('#fit-route')).toBeDisabled();
  expect(await page.locator('#fit-route').evaluate(el =>
    el.previousElementSibling?.id === 'fit-members' || el.nextElementSibling?.id === 'fit-members'),
  'Fit Route is beside Fit members').toBe(true);
  await captureFits(page);
  await searchSelect(page, fixture.feature);
  await expectReadyRoute(page, fixture);
  await expectRouteFits(page);
  await expect(page.locator('#selected-members')).toHaveText(fixture.feature.properties.members.toLocaleString('en-AU'));
  await expect(page.locator('#selected-distance')).toHaveText(`${(fixture.route.distanceM / 1000).toFixed(1)} km`);
  await expect(page.locator('#selected-group')).toContainText(fixture.group.name);
  await expect(page.locator('#selected-threshold')).toContainText(fixture.route.distanceM <= 50_000 ? 'Within' : 'Beyond');
  await expect(page.locator('#route-status')).toHaveClass(/sr-only/);
  await expect(page.locator('#route-status')).toHaveAttribute('role', 'status');
  await expect(page.locator('#route-status')).toHaveAttribute('aria-live', 'polite');
  await expect(page.locator('#selected-meta, #selected-flags, #group-context')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText(/Centroid to road:|Meeting point to road:|Offsets are not included in driving distance|members in source\s*·.*mapped/i);
  await expect.poll(() => page.evaluate(() => window.__routeFitCalls.at(-1)?.maxZoom)).toBe(17);
  const fitsBefore = await page.evaluate(() => window.__routeFitCalls.length);
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [131, -27], zoom: 12 }));
  await page.locator('#fit-route').click();
  await expect.poll(() => page.evaluate(() => window.__routeFitCalls.length)).toBeGreaterThan(fitsBefore);
  await expectReadyRoute(page, fixture);
  await expectRouteFits(page);
  expect(snapRequests, 'Selection does not request snap-report.json').toEqual([]);
  await page.locator('#close-selection').click();
  await expect(page.locator('#selected-suburb')).toBeHidden();
  await expect(page.locator('#fit-route')).toBeDisabled();
  expect(await page.evaluate(() => window.__markerTestMap.getStyle().sources.route.data.features)).toEqual([]);
});

for (const displayMode of ['density', 'points', 'bands']) {
  test(`closing a selected suburb restores its previous map view in ${displayMode} mode`, async ({ page }) => {
    const fixture = await routeFixture('ADELAIDE');
    await ready(page);
    await mode(page, displayMode);
    await page.locator('#group-layer-toggle').uncheck();
    await closeMap(page);
    await page.evaluate(coordinates => window.__markerTestMap.jumpTo({ center: coordinates, zoom: 12,
      bearing: 17, pitch: 25, padding: { top: 23, bottom: 31, left: 17, right: 29 } }), fixture.feature.geometry.coordinates);
    await expect.poll(() => page.evaluate(({ id, coordinates }) => {
      const map = window.__markerTestMap;
      return map.queryRenderedFeatures(map.project(coordinates), { layers: ['member-points'] }).some(feature => feature.properties.id === id);
    }, { id: fixture.feature.properties.id, coordinates: fixture.feature.geometry.coordinates })).toBe(true);
    const originalView = await cameraView(page);
    await captureFits(page);
    const point = await page.evaluate(coordinates => {
      const map = window.__markerTestMap;
      const position = map.project(coordinates);
      const bounds = map.getContainer().getBoundingClientRect();
      return [bounds.left + position.x, bounds.top + position.y];
    }, fixture.feature.geometry.coordinates);
    await page.mouse.click(...point);
    await expect(page.locator('#selected-name')).toHaveText('ADELAIDE');
    await expectReadyRoute(page, fixture);
    await expectRouteFits(page);
    await expect.poll(() => page.evaluate(() => window.__routeFitCalls.at(-1)?.maxZoom)).toBe(17);
    await expect(page.locator('.maplibregl-popup')).toHaveCount(0);
    await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [137, -33], zoom: 8, bearing: -8, pitch: 12 }));
    await page.locator('#close-selection').click();
    await expect(page.locator('#selected-suburb')).toBeHidden();
    await expect(page.locator('#fit-route')).toBeDisabled();
    await expectCameraView(page, originalView);
    expect(await page.evaluate(() => window.__markerTestMap.getStyle().sources.route.data.features)).toEqual([]);
  });
}

test('route changes and refitting preserve the original view, and a new selection saves a fresh view', async ({ page }) => {
  const first = await routeFixture('ADELAIDE');
  const second = await routeFixture('VICTOR HARBOR');
  await ready(page);
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [138.7, -33.9], zoom: 6, bearing: 9, pitch: 15 }));
  const originalView = await cameraView(page);
  await searchSelect(page, first.feature);
  await expectReadyRoute(page, first);
  await searchSelect(page, second.feature);
  await expectReadyRoute(page, second);
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [137, -31], zoom: 7 }));
  await page.locator('#fit-route').click();
  await expectRouteFits(page);
  await page.locator('#close-selection').click();
  await expectCameraView(page, originalView);

  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [139.3, -34.2], zoom: 7.1, bearing: -18, pitch: 20 }));
  const nextView = await cameraView(page);
  await searchSelect(page, first.feature);
  await expect(page.locator('#route-status')).toContainText('Road route ready');
  // Close while the cached route's automatic camera animation can still be running.
  await page.locator('#close-selection').click();
  await expectCameraView(page, nextView);
});

test('closing a selected suburb in cluster mode keeps the current map view', async ({ page }) => {
  const fixture = await routeFixture('ADELAIDE');
  await ready(page);
  await mode(page, 'clusters');
  await closeMap(page);
  await searchSelect(page, fixture.feature);
  await expectReadyRoute(page, fixture);
  const fittedView = await cameraView(page);
  await page.locator('#close-selection').click();
  await expectCameraView(page, fittedView);
});

test('responsive route fitting survives zoom in, zoom out and refitting on desktop and mobile', async ({ page }) => {
  const fixture = await routeFixture('ADELAIDE');
  await ready(page);
  for (const viewport of [
    { width: 1440, height: 1000 }, { width: 1280, height: 650 },
    { width: 390, height: 844 }, { width: 375, height: 500 },
  ]) {
    await page.setViewportSize(viewport);
    await searchSelect(page, fixture.feature);
    await expectReadyRoute(page, fixture);
    await expectRouteFits(page);
    for (const direction of ['in', 'out']) {
      const zoom = await page.evaluate(() => window.__markerTestMap.getZoom());
      await page.locator(`.maplibregl-ctrl-zoom-${direction}`).click();
      const actual = expect.poll(() => page.evaluate(() => window.__markerTestMap.getZoom()));
      if (direction === 'in') await actual.toBeGreaterThan(zoom);
      else await actual.toBeLessThan(zoom);
      await expect.poll(() => page.evaluate(() => window.__markerTestMap.isMoving())).toBe(false);
      await page.locator('#fit-route').click();
      await expectRouteFits(page);
      await expectReadyRoute(page, fixture);
    }
    await expect(page.locator('.maplibregl-popup')).toHaveCount(0);
  }
});

test('no-route selection leaves Fit Route disabled and never fits an absent line', async ({ page }) => {
  const fixture = await routeFixture('ADELAIDE');
  const analysis = structuredClone(sourceAnalysis);
  for (const group of analysis.groups) {
    analysis.distances[fixture.feature.properties.id][group.id] = { status: 'unsnapped', distanceM: null, usesFerry: false };
  }
  await page.route('**/public/data/analysis.json', route => route.fulfill({ json: analysis }));
  const geometryRequests = [];
  page.on('request', request => { if (request.url().includes('/public/data/routes/')) geometryRequests.push(request.url()); });
  await ready(page);
  await captureFits(page);
  await searchSelect(page, fixture.feature);
  await expect(page.locator('#selected-threshold')).toContainText('No computed');
  await expect(page.locator('#selected-distance')).toHaveText('No route');
  await expect(page.locator('#route-status')).toContainText('No computed road route');
  await expect(page.locator('#fit-route')).toBeDisabled();
  expect(await page.evaluate(() => window.__routeFitCalls)).toEqual([]);
  expect(await page.evaluate(() => window.__markerTestMap.getStyle().sources.route.data.features)).toEqual([]);
  expect(geometryRequests).toEqual([]);
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [138.8, -34.7], zoom: 8 }));
  const currentView = await cameraView(page);
  await page.locator('#close-selection').click();
  await expectCameraView(page, currentView);
});

test('route loading failure leaves Fit Route disabled and selecting again retries successfully', async ({ page }) => {
  const fixture = await routeFixture('ADELAIDE');
  const pattern = `**/public/data/routes/${fixture.route.groupId}.geojson`;
  await page.route(pattern, route => route.fulfill({ status: 503, body: 'Unavailable' }));
  await ready(page);
  await captureFits(page);
  await searchSelect(page, fixture.feature);
  await expect(page.locator('#route-status')).toContainText(/route.*could not load/i);
  await expect(page.locator('#selected-distance')).toHaveText(`${(fixture.route.distanceM / 1000).toFixed(1)} km`);
  await expect(page.locator('#fit-route')).toBeDisabled();
  expect(await page.evaluate(() => window.__routeFitCalls)).toEqual([]);
  expect(await page.evaluate(() => window.__markerTestMap.getStyle().sources.route.data.features)).toEqual([]);
  await page.evaluate(() => window.__markerTestMap.jumpTo({ center: [138.75, -34.6], zoom: 8, bearing: 2, pitch: 20 }));
  const retryView = await cameraView(page);
  await page.unroute(pattern);
  await searchSelect(page, fixture.feature);
  await expectReadyRoute(page, fixture);
  await expectRouteFits(page);
  await page.locator('#close-selection').click();
  await expectCameraView(page, retryView);
});

test('a late route response cannot replace the latest selection, status, or fitted camera', async ({ page }) => {
  const first = await routeFixture('ADELAIDE');
  const latest = await routeFixture('VICTOR HARBOR');
  expect(first.route.groupId).not.toBe(latest.route.groupId);
  let release, notifyStarted, notifyFinished;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { notifyStarted = resolve; });
  const finished = new Promise(resolve => { notifyFinished = resolve; });
  await page.route(`**/public/data/routes/${first.route.groupId}.geojson`, async route => {
    notifyStarted();
    await held;
    try {
      await route.fulfill({ json: first.collection });
    } catch (error) {
      // Aborting a superseded browser request is also a valid outcome.
      if (!/closed|cancel|abort|invalid interception/i.test(error.message)) throw error;
    } finally {
      notifyFinished();
    }
  });
  try {
    await ready(page);
    await captureFits(page);
    await searchSelect(page, first.feature);
    await started;
    await expect(page.locator('#route-status')).toContainText('Loading road route');
    await expect(page.locator('#fit-route')).toBeDisabled();
    await searchSelect(page, latest.feature);
    await expectReadyRoute(page, latest);
    await expectRouteFits(page);
    const camera = await page.evaluate(() => ({ center: window.__markerTestMap.getCenter().toArray(), zoom: window.__markerTestMap.getZoom() }));
    const fits = await page.evaluate(() => window.__routeFitCalls.length);
    release();
    await finished;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.locator('#selected-name')).toHaveText(latest.feature.properties.name);
    await expectReadyRoute(page, latest);
    expect(await page.evaluate(() => ({ center: window.__markerTestMap.getCenter().toArray(), zoom: window.__markerTestMap.getZoom() }))).toEqual(camera);
    expect(await page.evaluate(() => window.__routeFitCalls.length)).toBe(fits);
  } finally {
    release();
  }
});
