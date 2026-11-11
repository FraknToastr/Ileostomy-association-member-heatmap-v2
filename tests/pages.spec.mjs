import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';

const prefix = '/Ileostomy-association-member-heatmap-v2/';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');

async function instrument(page, baseURL) {
  const origin = new URL(baseURL).origin;
  const diagnostics = { local: [], failures: [], external: [], errors: [], glyphs: [] };
  page.on('pageerror', error => diagnostics.errors.push(error.message));
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.origin === origin) diagnostics.local.push({ path: url.pathname, status: response.status() });
  });
  page.on('requestfailed', request => {
    if (new URL(request.url()).origin === origin) diagnostics.failures.push(request.url());
  });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.protocol.startsWith('http') && url.origin !== origin) {
      diagnostics.external.push(url.href);
      return route.abort();
    }
    return route.continue();
  });
  await page.route(/https:\/\/(tile\.openstreetmap\.org|services\.arcgisonline\.com)\/.+/, route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: png }));
  await page.route('https://demotiles.maplibre.org/font/**', async route => {
    const parts = new URL(route.request().url()).pathname.split('/').map(decodeURIComponent);
    expect(parts.slice(0, -1)).toEqual(['', 'font', 'Noto Sans Regular']);
    const range = parts.at(-1);
    expect(range).toMatch(/^\d+-\d+\.pbf$/);
    const fixture = new URL(`../.generated/test-glyphs/Noto%20Sans%20Regular/${range}`, import.meta.url);
    const body = await readFile(fixture);
    await route.fulfill({ status: 200, contentType: 'application/x-protobuf',
      headers: { 'access-control-allow-origin': '*' }, body });
    diagnostics.glyphs.push(range);
  });
  await page.addInitScript(() => {
    let library;
    window.__pagesMapErrors = [];
    // As in the normal map suite, intercept the real UMD library assignment.
    Object.defineProperty(window, 'maplibregl', {
      configurable: true, get: () => library,
      set(value) {
        library = value;
        queueMicrotask(() => {
          value.Map = new Proxy(value.Map, {
            construct(Constructor, args) {
              const map = Reflect.construct(Constructor, args);
              window.__pagesMap = map;
              map.on('error', event => window.__pagesMapErrors.push(event.error?.message || String(event.error)));
              return map;
            },
          });
        });
      },
    });
  });
  return diagnostics;
}

async function menu(page, name) {
  const panel = page.locator(`#${name}-panel`);
  if (!await panel.evaluate(element => element.matches(':popover-open'))) await page.locator(`#${name}-toggle`).click();
  await expect(panel).toBeVisible();
}

async function camera(page) {
  return page.evaluate(() => {
    const map = window.__pagesMap;
    const round = value => Number(value.toFixed(6));
    return { center: map.getCenter().toArray().map(round), zoom: round(map.getZoom()),
      bearing: round(map.getBearing()), pitch: round(map.getPitch()),
      padding: Object.fromEntries(Object.entries(map.getPadding()).map(([key, value]) => [key, round(value)])) };
  });
}

async function routeFits(page, endpoints) {
  await expect.poll(() => page.evaluate(extra => {
    const map = window.__pagesMap;
    const geometry = map.getStyle().sources.route.data.features[0]?.geometry;
    if (!geometry || map.isMoving()) return false;
    const coordinates = geometry.type === 'MultiLineString' ? geometry.coordinates.flat() : geometry.coordinates;
    const points = [...coordinates, ...extra].map(coordinate => map.project(coordinate));
    const { width, height } = map.getContainer().getBoundingClientRect();
    const xs = points.map(point => point.x), ys = points.map(point => point.y);
    const padding = map.getPadding();
    const fill = Math.max((Math.max(...xs) - Math.min(...xs)) / Math.max(1, width - padding.left - padding.right),
      (Math.max(...ys) - Math.min(...ys)) / Math.max(1, height - padding.top - padding.bottom));
    return coordinates.length > 1 && Math.min(...xs) >= -1 && Math.min(...ys) >= -1
      && Math.max(...xs) <= width + 1 && Math.max(...ys) <= height + 1 && fill > 0.3 && map.getZoom() <= 17.001;
  }, endpoints), { message: 'Complete built route and both endpoints fit the map at a useful scale' }).toBe(true);
}

for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
  test.describe(`GitHub Pages ${name}`, () => {
    test.use({ viewport, reducedMotion: 'reduce' });
    test('built map, controls, labels and nested routes work at the project prefix', async ({ page, request, baseURL }) => {
      // Fixtures come from dist through HTTP, never from the development data tree.
      const getJSON = async relative => {
        const response = await request.get(new URL(relative, baseURL).href);
        expect(response.status(), relative).toBe(200);
        return response.json();
      };
      const analysis = await getJSON('public/data/analysis.json');
      const members = (await getJSON('public/data/members.geojson')).features;
      const suburb = members.find(feature => feature.properties.name === 'CEDUNA');
      expect(suburb).toBeTruthy();
      const [nearest] = analysis.groups.map(group => ({ group, ...analysis.distances[suburb.properties.id][group.id] }))
        .filter(route => route.status === 'ok').sort((a, b) => a.distanceM - b.distanceM);
      expect(nearest).toBeTruthy();
      const routePath = `public/data/routes/${nearest.group.id}.geojson`;
      const routeData = await getJSON(routePath);
      const line = routeData.features.find(feature => feature.properties.suburbId === suburb.properties.id);
      expect(line).toBeTruthy();
      const diagnostics = await instrument(page, baseURL);
      await page.goto(baseURL);
      await expect(page).toHaveURL(baseURL);
      await expect(page).toHaveTitle('Ileostomy Association Member Heatmap');
      for (const [id, count] of [['joined-total', '3,019'], ['unresolved-total', '86'], ['table-member-total', '3,019']]) {
        await expect(page.locator(`#${id}`)).toHaveText(count);
      }
      await expect(page.locator('#data-notice')).toBeHidden();
      await expect(page.locator('#map-notice')).toBeHidden();
      await expect(page.locator('.maplibregl-canvas')).toBeVisible();
      await expect(page.locator('.group-marker')).toHaveCount(11);
      await expect(page.locator('#fit-route')).toBeDisabled();
      await expect.poll(() => page.evaluate(() => window.__pagesMap?.loaded())).toBe(true);
      const visibility = layer => page.evaluate(id => window.__pagesMap.getLayoutProperty(id, 'visibility'), layer);
      await expect.poll(() => visibility('member-points')).toBe('visible');
      expect(await visibility('member-heat')).toBe('none');
      expect(await visibility('member-cluster-circles')).toBe('none');
      await expect.poll(() => page.evaluate(() => window.__pagesMap.queryRenderedFeatures({ layers: ['member-points'] }).length)).toBeGreaterThan(0);
      const initialZoom = await page.evaluate(() => window.__pagesMap.getZoom());
      await page.locator('.maplibregl-ctrl-zoom-in').click();
      await expect.poll(() => page.evaluate(() => window.__pagesMap.getZoom())).toBeGreaterThan(initialZoom);
      await page.locator('#fit-members').click();
      await expect.poll(() => page.evaluate(() => window.__pagesMap.isMoving())).toBe(false);
      await menu(page, 'map-controls');
      await expect(page.locator('input[name="map-mode"][value="points"]')).toBeChecked();
      await expect(page.locator('#map-controls-heading')).toBeFocused();
      await page.locator('#map-controls-close').click();
      await expect(page.locator('#map-controls-toggle')).toBeFocused();

      await page.evaluate(() => window.__pagesMap.jumpTo({ center: [138.7, -33.9], zoom: 6, bearing: 9, pitch: 15 }));
      const original = await camera(page);
      await page.locator('#suburb-search').fill(suburb.properties.name);
      await page.locator('#search-results').selectOption(suburb.properties.id);
      await expect(page.locator('#selected-name')).toHaveText(suburb.properties.name);
      await expect(page.locator('#selected-members')).toHaveText(suburb.properties.members.toLocaleString('en-AU'));
      await expect(page.locator('#selected-distance')).toHaveText(`${(nearest.distanceM / 1000).toFixed(1)} km`);
      await expect(page.locator('#route-status')).toContainText(`Road route ready · ${nearest.group.name}`);
      await expect(page.locator('#fit-route')).toBeEnabled();
      await expect.poll(() => page.evaluate(() => window.__pagesMap.getStyle().sources.route.data.features[0]?.geometry)).toEqual(line.geometry);
      const endpoints = [suburb.geometry.coordinates, nearest.group.coordinates];
      await routeFits(page, endpoints);
      const selectedCamera = await camera(page);
      for (const id of ['topo', 'aerial', 'osm']) {
        await menu(page, 'map-controls');
        await page.locator('#basemap-select').selectOption(id);
        expect(await visibility(`basemap-${id}`)).toBe('visible');
        expect(await camera(page)).toEqual(selectedCamera);
        await expect(page.locator('.maplibregl-ctrl-attrib')).toContainText(id === 'osm' ? 'OpenStreetMap' : id === 'topo' ? 'Esri World Topography' : 'Esri World Imagery');
      }
      await page.locator('#map-controls-close').click();
      await page.evaluate(() => window.__pagesMap.jumpTo({ center: [131, -27], zoom: 12 }));
      await page.locator('#fit-route').click();
      await routeFits(page, endpoints);
      await page.locator('#close-selection').click();
      await expect(page.locator('#selected-suburb')).toBeHidden();
      await expect(page.locator('#fit-route')).toBeDisabled();
      await expect.poll(() => camera(page)).toEqual(original);
      expect(await page.evaluate(() => window.__pagesMap.getStyle().sources.route.data.features)).toEqual([]);

      await menu(page, 'map-controls');
      await page.locator('#group-label-toggle').uncheck();
      await expect(page.locator('.group-name').first()).toBeHidden();
      await page.locator('#group-label-toggle').check();
      await expect(page.locator('.group-name').first()).toBeVisible();
      await page.locator('#suburb-label-toggle').check();
      await page.evaluate(() => window.__pagesMap.jumpTo({ center: [138.6, -34.93], zoom: 10, bearing: 0, pitch: 0 }));
      await expect.poll(() => page.evaluate(() => window.__pagesMap.queryRenderedFeatures({ layers: ['suburb-labels'] }).length)).toBeGreaterThan(0);
      await page.locator('input[name="map-mode"][value="clusters"]').check();
      await page.evaluate(() => window.__pagesMap.jumpTo({ center: [138.6, -34.93], zoom: 7 }));
      await expect.poll(() => page.evaluate(() => window.__pagesMap.queryRenderedFeatures({ layers: ['cluster-count-labels'] }).length)).toBeGreaterThan(0);
      await page.locator('#cluster-label-toggle').uncheck();
      expect(await visibility('cluster-count-labels')).toBe('none');
      await page.locator('#cluster-label-toggle').check();
      expect(await visibility('cluster-count-labels')).toBe('visible');
      await page.locator('input[name="map-mode"][value="density"]').check();
      expect(await visibility('member-heat')).toBe('visible');
      expect(await visibility('member-cluster-circles')).toBe('none');
      await expect(page.locator('#density-legend')).toBeVisible();
      await page.locator('input[name="map-mode"][value="bands"]').check();
      expect(await visibility('member-heat')).toBe('none');
      expect(await visibility('member-points')).toBe('visible');
      await expect(page.locator('#band-legend')).toBeVisible();
      await page.locator('#member-layer-toggle').uncheck();
      expect(await visibility('member-points')).toBe('none');
      await page.locator('#member-layer-toggle').check();
      await page.locator('#map-controls-close').click();
      await page.locator('#band-rows .band-button').first().click();
      await menu(page, 'display-filters');
      await expect(page.locator('#visible-members')).toHaveText('2,286');
      await page.locator('#clear-filters').click();
      await expect(page.locator('#visible-members')).toHaveText('3,019');
      await expect(page.locator('#table-member-total')).toHaveText('3,019');
      await page.locator('#display-filters-close').click();
      await page.locator('#theme-toggle').click();
      await expect(page.locator('#theme-toggle')).toHaveAttribute('aria-pressed', 'true');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);

      for (const relative of ['app.js', 'styles.css', 'src/map-style.js', 'public/vendor/maplibre-gl.js',
        'public/data/members.geojson', 'public/data/analysis.json', 'public/data/membership-report.json', routePath]) {
        expect(diagnostics.local, `Built asset loaded beneath prefix: ${relative}`).toContainEqual({ path: prefix + relative, status: 200 });
      }
      expect(diagnostics.local.every(response => response.path.startsWith(prefix)), 'All local requests keep the Pages prefix').toBe(true);
      expect(diagnostics.local.filter(response => response.status >= 400), 'No missing local assets').toEqual([]);
      expect(diagnostics.failures).toEqual([]);
      expect(diagnostics.external, 'External tiles and glyphs are fulfilled locally').toEqual([]);
      expect(diagnostics.glyphs.length, 'Labels used actual cached glyph bytes').toBeGreaterThan(0);
      expect(diagnostics.errors).toEqual([]);
      expect(await page.evaluate(() => window.__pagesMapErrors)).toEqual([]);
      await expect(page.locator('.maplibregl-popup')).toHaveCount(0);
    });
  });
}

function rawResponse(baseURL, pathname, method = 'GET') {
  return new Promise((resolve, reject) => {
    const request = httpRequest(new URL(baseURL).origin, { path: pathname, method }, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body }));
    });
    request.on('error', reject);
    request.end();
  });
}

test('Pages preview limits methods, paths and private source files to the built prefix', async ({ request, baseURL }) => {
  const redirect = await request.get(new URL(`${prefix.slice(0, -1)}?preview=1`, baseURL).href, { maxRedirects: 0 });
  expect(redirect.status()).toBe(308);
  expect(redirect.headers().location).toBe(`${prefix}?preview=1`);
  for (const [relative, type] of [['', 'text/html'], ['styles.css', 'text/css'], ['app.js', 'text/javascript'],
    ['src/map-style.js', 'text/javascript'], ['public/data/analysis.json', 'application/json'],
    ['public/data/members.geojson', 'application/geo+json']]) {
    const url = new URL(relative, baseURL).href;
    const get = await request.get(url);
    const head = await request.head(url);
    expect(get.status(), relative).toBe(200);
    expect(head.status(), relative).toBe(200);
    expect(get.headers()['content-type']).toContain(type);
    expect(head.headers()['content-type']).toBe(get.headers()['content-type']);
    expect(head.headers()['content-length']).toBe(String((await get.body()).length));
    expect(await head.body()).toHaveLength(0);
    expect(get.headers()['x-content-type-options']).toBe('nosniff');
  }
  // Node's HTTP client preserves traversal spellings that URL/browser clients normalize.
  for (const pathname of ['/', '/app.js', '/public/data/analysis.json', '/dist/index.html', `${prefix}missing.js`,
    `${prefix}public/data/`, `${prefix}../package.json`, `${prefix}%2e%2e/package.json`,
    `${prefix}public/data/../../../../package.json`, `${prefix}%252e%252e/package.json`, `${prefix}.git/config`,
    `${prefix}.generated/network.json`, `${prefix}public/data/network.json`, `${prefix}package.json`,
    `${prefix}Heat%20Map%20Data%20October%202026.xlsx`, `${prefix}south-australia-261108.osm.pbf`,
    `${prefix}public/data/Heat%20Map%20Data%20October%202026.xlsx`, `${prefix}public/data/south-australia-261108.osm.pbf`]) {
    expect((await rawResponse(baseURL, pathname)).status, pathname).toBe(404);
  }
  for (const pathname of [`${prefix}%`, `${prefix}%00index.html`, `${prefix}%5c..%5cpackage.json`, `${prefix}public%2fdata/analysis.json`]) {
    expect((await rawResponse(baseURL, pathname)).status, pathname).toBe(400);
  }
  const post = await rawResponse(baseURL, prefix, 'POST');
  expect(post.status).toBe(405);
  expect(post.headers.allow).toBe('GET, HEAD');
  const missing = await rawResponse(baseURL, `${prefix}missing.js`, 'HEAD');
  expect(missing.status).toBe(404);
  expect(missing.body).toBe('');
});
