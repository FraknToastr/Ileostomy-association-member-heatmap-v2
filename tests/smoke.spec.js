import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');

test.beforeEach(async ({ page }) => {
  await page.route(/https:\/\/(tile\.openstreetmap\.org|services\.arcgisonline\.com)\/.+/, route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: png }));
});

async function ready(page) {
  await page.goto('/');
  await expect(page.locator('#joined-total')).toHaveText('3,019');
  await expect(page.locator('#unresolved-total')).toHaveText('86');
  await expect(page.locator('#export-csv')).toBeEnabled();
  await expect(page.locator('#data-notice')).toBeHidden();
}

async function openFilters(page) {
  if (!await page.locator('#display-filters-panel').evaluate(el => el.matches(':popover-open'))) {
    await page.locator('#display-filters-toggle').click();
  }
  await expect(page.locator('#display-filters-panel')).toBeVisible();
}

async function openMap(page) {
  if (!await page.locator('#map-controls-panel').evaluate(el => el.matches(':popover-open'))) {
    await page.locator('#map-controls-toggle').click();
  }
  await expect(page.locator('#map-controls-panel')).toBeVisible();
  await expect(page.locator('#display-filters-panel')).toBeHidden();
}

test('MapLibre app loads real member counts and reconciles the band table', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await ready(page);
  await expect(page).toHaveTitle('Ileostomy Association Member Heatmap');
  await expect(page.locator('.maplibregl-canvas')).toBeVisible();
  await expect(page.locator('header #map-controls-toggle')).toHaveText('Map');
  await expect(page.locator('header #display-filters-toggle')).toHaveText('Display Filters');
  await expect(page.locator('#fit-route')).toBeDisabled();
  await expect(page.locator('#selected-meta, #selected-flags, #group-context')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText(/members in source\s*·.*mapped\s*·.*awaiting a suburb match/i);
  for (const copy of [
    'Appearance changes do not change member totals.',
    'Suburb points are aggregate locations.',
    'Larger circles represent more members.',
    'Select a cluster for its member and suburb breakdown.',
    'Explore member access to a selected group or the nearest group by road.',
    'Outlined suburbs are beyond the threshold; they remain on the map.',
    'Each suburb is assigned once, to its closest reachable group by road.',
  ]) await expect(page.locator('body')).not.toContainText(copy);
  await expect(page.locator('#table-member-total')).toContainText('3,019');
  const totals = await page.locator('#band-rows tr').allTextContents();
  expect(totals.length).toBeGreaterThanOrEqual(6);
  expect(errors).toEqual([]);
});

test('group selection, editable boundaries, filtering and search operate on the same totals', async ({ page }) => {
  await ready(page);
  await page.locator('input[name="analysis-mode"][value="group"]').check();
  await page.locator('#group-select').selectOption('group-1');
  await expect(page.locator('#table-member-total')).toContainText('3,019');
  await page.getByText('Edit distance bands', { exact: true }).click();
  await page.locator('#band-boundaries').fill('10, 30, 60, 150');
  await page.locator('#band-form button[type="submit"]').click();
  await expect(page.locator('#band-error')).toBeHidden();
  await expect(page.locator('#band-rows')).toContainText('30');
  await page.locator('#band-boundaries').fill('50, 25');
  await page.locator('#band-form button[type="submit"]').click();
  await expect(page.locator('#band-error')).toBeVisible();
  await expect(page.locator('#table-member-total')).toContainText('3,019');
  await page.locator('#suburb-search').fill('Adelaide');
  await expect(page.locator('#search-results')).toBeVisible();
  await page.locator('#search-results').selectOption({ index: 0 });
  await expect(page.locator('#selected-suburb')).toBeVisible();
  await expect(page.locator('#selected-members')).not.toHaveText('—');
  await expect(page.locator('#route-status')).toContainText('Road route ready', { timeout: 30_000 });
  await expect(page.locator('#route-status')).toHaveClass(/sr-only/);
  await expect(page.locator('#fit-route')).toBeEnabled();
  await expect(page.locator('.maplibregl-popup')).toHaveCount(0);
});

test('CSV export includes every joined member and source files cannot be served', async ({ page, request }) => {
  await ready(page);
  const downloading = page.waitForEvent('download');
  await page.locator('#export-csv').click();
  const download = await downloading;
  const csv = await fs.readFile(await download.path(), 'utf8');
  expect(csv).toContain('3019');
  expect(csv).toContain('86');
  expect((await request.get('/Heat%20Map%20Data%20October%202026.xlsx')).status()).toBe(404);
  expect((await request.get('/.generated/network.json')).status()).toBe(404);
  expect((await request.get('/public/data/../../package.json')).status()).toBe(404);
});

test('distance-band and range filters update visible members without changing the denominator', async ({ page }) => {
  await ready(page);
  await page.locator('#band-rows .band-button').first().click();
  await openFilters(page);
  await expect(page.locator('#visible-members')).toHaveText('2,286');
  await expect(page.locator('#table-member-total')).toHaveText('3,019');
  await page.locator('#clear-filters').click();
  await page.locator('#distance-min').fill('0');
  await page.locator('#distance-max').fill('25');
  await page.locator('#range-form button[type="submit"]').click();
  await expect(page.locator('#visible-members')).toHaveText('2,286');
  await openMap(page);
  await page.locator('input[name="map-mode"][value="bands"]').check();
  await expect(page.locator('#band-legend')).toBeVisible();
  await page.keyboard.press('Escape');
  await page.locator('.maplibregl-ctrl-zoom-in').click();
  await expect(page.locator('#table-member-total')).toHaveText('3,019');
});

test('data load failure offers retry and recovers', async ({ page }) => {
  await page.route('**/public/data/members.geojson', route => route.fulfill({ status: 503, body: 'Unavailable' }));
  await page.goto('/');
  await expect(page.locator('#retry-data')).toBeVisible();
  await page.unroute('**/public/data/members.geojson');
  await page.locator('#retry-data').click();
  await expect(page.locator('#joined-total')).toHaveText('3,019');
  await expect(page.locator('#data-notice')).toBeHidden();
});

test('mobile layout fits the viewport and theme control works', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await ready(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1
    && document.documentElement.scrollHeight <= innerHeight + 1)).toBe(true);
  await page.locator('#theme-toggle').click();
  await expect(page.locator('#theme-toggle')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: 'test-results/mobile.png', fullPage: true });
});
