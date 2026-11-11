import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';

const dataRoot = new URL('../public/data/', import.meta.url);
const [analysis, memberData, report] = await Promise.all(
  ['analysis.json', 'members.geojson', 'membership-report.json'].map(async name =>
    JSON.parse(await fs.readFile(new URL(name, dataRoot), 'utf8'))),
);
const members = memberData.features;
const central = analysis.groups.find(group => group.name === 'Central');
const columns = ['Suburb/Locality', 'Postcode', 'Members', 'Road distance (km)',
  'Support group', 'Analysis mode', 'Distance threshold (km)'];
const exportIds = ['export-csv', 'export-within', 'export-beyond'];
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
const browserLogs = new WeakMap();

test.beforeEach(async ({ page }) => {
  const logs = { errors: [], expectMembers503: false };
  browserLogs.set(page, logs);
  page.on('pageerror', error => logs.errors.push({ kind: 'pageerror', text: error.message }));
  page.on('console', message => {
    if (message.type() === 'error') logs.errors.push({
      kind: 'console', text: message.text(), url: message.location().url,
    });
  });
  await page.route(/https:\/\/(tile\.openstreetmap\.org|services\.arcgisonline\.com)\/.+/, route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: png }));
  await page.route('https://demotiles.maplibre.org/font/**', async route => {
    const name = new URL(route.request().url()).pathname.split('/').pop();
    if (!['0-255.pbf', '256-511.pbf', '8192-8447.pbf'].includes(name)) return route.abort();
    const body = await fs.readFile(new URL(`../.generated/test-glyphs/Noto Sans Regular/${name}`, import.meta.url));
    await route.fulfill({ contentType: 'application/x-protobuf',
      headers: { 'access-control-allow-origin': '*' }, body });
  });
});

test.afterEach(async ({ page }) => {
  const logs = browserLogs.get(page);
  const unexpected = logs.errors.filter(error => !(logs.expectMembers503
    && error.kind === 'console' && error.url?.endsWith('/public/data/members.geojson')
    && /Failed to load resource:.*503/.test(error.text)));
  expect(unexpected, 'No unexpected browser console errors or uncaught exceptions').toEqual([]);
});

async function ready(page) {
  await expect(page.locator('html')).toHaveAttribute('data-app-ready', 'true');
  await expect(page.locator('#joined-total')).toHaveText(report.totals.joinedMembers.toLocaleString('en-AU'));
  await expect(page.locator('#data-notice')).toBeHidden();
}

async function expectHeaderFits(page) {
  const layout = await page.evaluate(() => {
    const header = document.querySelector('header').getBoundingClientRect();
    return {
      width: innerWidth, height: innerHeight,
      documentWidth: document.documentElement.scrollWidth,
      documentHeight: document.documentElement.scrollHeight,
      buttons: [...document.querySelectorAll('header nav button')].map(button => {
        const rect = button.getBoundingClientRect();
        return { id: button.id, left: rect.left, right: rect.right,
          top: rect.top, bottom: rect.bottom, headerBottom: header.bottom };
      }),
    };
  });
  expect(layout.buttons.map(button => button.id)).toEqual([
    'map-controls-toggle', 'display-filters-toggle', 'theme-toggle', 'export-toggle',
  ]);
  expect(layout.documentWidth, 'No horizontal document overflow').toBeLessThanOrEqual(layout.width + 1);
  expect(layout.documentHeight, 'The document stays inside the viewport').toBeLessThanOrEqual(layout.height + 1);
  for (const button of layout.buttons) {
    expect(button.left, button.id).toBeGreaterThanOrEqual(0);
    expect(button.right, button.id).toBeLessThanOrEqual(layout.width + 1);
    expect(button.bottom, button.id).toBeLessThanOrEqual(button.headerBottom + 1);
    expect(button.right - button.left, button.id).toBeGreaterThan(0);
  }
  for (let index = 1; index < layout.buttons.length; index++) {
    const before = layout.buttons[index - 1], after = layout.buttons[index];
    expect(after.top >= before.bottom - 1 || after.left >= before.right - 1,
      'Header buttons wrap without overlapping').toBe(true);
  }
}

async function openExport(page) {
  await page.locator('#export-toggle').click();
  const panel = page.locator('#export-panel');
  await expect(panel).toBeVisible();
  await expect(page.locator('#export-toggle')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#export-heading')).toBeFocused();
  expect(await panel.evaluate(element => {
    const rect = element.getBoundingClientRect();
    return rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth + 1
      && rect.bottom <= innerHeight + 1 && element.scrollWidth <= element.clientWidth + 1;
  }), 'Export menu fits the viewport').toBe(true);
}

// All report fields are quoted. Keep parsing independent of the application's CSV helpers.
function parseCsv(csv) {
  expect(csv.endsWith('\r\n')).toBe(true);
  expect(csv.replaceAll('\r\n', '')).not.toMatch(/[\r\n]/);
  return csv.slice(0, -2).split('\r\n').map(line => {
    if (!line) return [];
    const fields = [...line.matchAll(/"((?:[^"]|"")*)"(?:,|$)/g)];
    expect(fields.map(match => match[0]).join(''), 'Every CSV field is quoted').toBe(line);
    return fields.map(([, field]) => field.replaceAll('""', '"'));
  });
}

async function downloadCsv(page, id, filename) {
  await openExport(page);
  const downloaded = page.waitForEvent('download');
  await page.locator(`#${id}`).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe(filename);
  const bytes = await fs.readFile(await download.path());
  expect([...bytes.subarray(0, 3)], 'CSV starts with a UTF-8 BOM').toEqual([0xef, 0xbb, 0xbf]);
  await expect(page.locator('#export-panel')).toBeHidden();
  await expect(page.locator('#export-toggle')).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('#export-toggle')).toBeFocused();
  return parseCsv(bytes.toString('utf8').slice(1));
}

// Compute assignments from the published road-distance matrix, without importing production code.
function expectedAssignments(groupId = null) {
  return members.filter(feature => Number.isFinite(feature.properties.members) && feature.properties.members > 0)
    .flatMap(feature => {
      const candidates = analysis.groups.filter(group => !groupId || group.id === groupId)
        .map(group => ({ group, route: analysis.distances[feature.properties.id]?.[group.id] }))
        .filter(({ route }) => route?.status === 'ok' && Number.isFinite(route.distanceM) && route.distanceM >= 0)
        .sort((a, b) => a.route.distanceM - b.route.distanceM
          || (a.group.id < b.group.id ? -1 : a.group.id > b.group.id ? 1 : 0));
      return candidates.length ? [{ ...feature.properties,
        distanceM: candidates[0].route.distanceM, groupName: candidates[0].group.name }] : [];
    }).sort((a, b) => a.name.localeCompare(b.name, 'en-AU')
      || a.postcode.localeCompare(b.postcode, 'en-AU') || a.id.localeCompare(b.id, 'en-AU'));
}

async function expectSuburbReports(page, km, groupId = null) {
  const assignments = expectedAssignments(groupId);
  let exportedMembers = 0;
  for (const category of ['within', 'beyond']) {
    const expected = assignments.filter(row => category === 'within'
      ? row.distanceM <= km * 1000 : row.distanceM > km * 1000);
    expect(expected.length, `${category} fixture contains suburbs`).toBeGreaterThan(0);
    const rows = await downloadCsv(page, `export-${category}`,
      `member-suburbs-${category}-${km}km-${groupId ?? 'nearest'}.csv`);
    expect(rows[0]).toEqual(columns);
    expect(rows.slice(1)).toEqual(expected.map(row => [row.name, row.postcode, String(row.members),
      String(row.distanceM / 1000), row.groupName, groupId ? 'Selected group' : 'Nearest group', String(km)]));
    const total = rows.slice(1).reduce((sum, row) => sum + Number(row[2]), 0);
    expect(total).toBe(expected.reduce((sum, row) => sum + row.members, 0));
    await expect(page.locator(`#threshold-${category}`)).toHaveText(total.toLocaleString('en-AU'));
    exportedMembers += total;
  }
  const noRouteMembers = report.totals.joinedMembers - assignments.reduce((sum, row) => sum + row.members, 0);
  expect(noRouteMembers, 'Fixture exercises exclusion of members without a road route').toBeGreaterThan(0);
  expect(exportedMembers + noRouteMembers).toBe(report.totals.joinedMembers);
  await expect(page.locator('#threshold-no-route')).toHaveText(noRouteMembers.toLocaleString('en-AU'));
}

async function expectBandReport(page, groupId = null) {
  const rows = await downloadCsv(page, 'export-csv', `member-distance-bands-${groupId ?? 'nearest'}.csv`);
  expect(rows[0]).toEqual(['Mode', 'Support group', 'Distance band (km)', 'Lower bound (inclusive)',
    'Upper bound (exclusive)', 'Members', 'Suburbs', 'Share of joined members (%)', 'Joined member denominator']);
  const bands = rows.slice(1).filter(row => row.length === 9);
  expect(bands.length).toBe(8);
  expect(bands.reduce((sum, row) => sum + Number(row[5]), 0)).toBe(report.totals.joinedMembers);
  for (const row of bands) {
    expect(row[0]).toBe(groupId ? 'Selected group' : 'Nearest group');
    expect(row[1]).toBe(groupId ? central.name : 'Nearest per suburb');
    expect(row[8]).toBe(String(report.totals.joinedMembers));
  }
  const sourceIndex = rows.findIndex(row => row[0] === 'Source reconciliation');
  expect(sourceIndex).toBeGreaterThan(0);
  expect(rows.slice(sourceIndex)).toEqual([
    ['Source reconciliation', 'Members'], ['Source records', String(report.totals.memberRecords)],
    ['Joined members', String(report.totals.joinedMembers)],
    ['Unresolved members (excluded from bands)', String(report.totals.unresolvedMembers)],
  ]);
}

async function filterFirstBand(page) {
  const band = page.locator('#band-rows .band-button').first();
  await band.click();
  await expect(band).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#display-filters-toggle').click();
  await expect(page.locator('#visible-members')).not.toHaveText(report.totals.joinedMembers.toLocaleString('en-AU'));
  await page.keyboard.press('Escape');
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 320, height: 640 }]) {
  test.describe(`CSV exports at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test('header menu preserves order, disables failed data, manages focus and keeps the band report', async ({ page }) => {
      const memberUrl = '**/public/data/members.geojson';
      browserLogs.get(page).expectMembers503 = true;
      await page.route(memberUrl, route => route.fulfill({ status: 503, body: 'Unavailable' }));
      await page.goto('/');
      await expect(page.locator('#retry-data')).toBeVisible();
      await expectHeaderFits(page);
      await expect(page.locator('header #export-toggle')).toHaveText('Export CSV');
      await expect(page.locator('#export-toggle')).toHaveAttribute('popovertarget', 'export-panel');
      await expect(page.locator('#export-toggle')).toHaveAttribute('aria-controls', 'export-panel');
      await expect(page.locator('#export-panel')).toHaveAttribute('popover', 'auto');
      await expect(page.locator('#export-panel')).toHaveAttribute('role', 'dialog');
      await expect(page.locator('#export-panel')).toHaveAttribute('aria-labelledby', 'export-heading');
      await expect(page.locator('#export-heading')).toHaveAttribute('tabindex', '-1');
      await openExport(page);
      for (const id of exportIds) await expect(page.locator(`#${id}`)).toBeDisabled();
      await expect(page.locator('#export-csv')).toHaveText('Distance-band summary');
      await expect(page.locator('#export-within')).toHaveText('Suburbs within 50 km');
      await expect(page.locator('#export-beyond')).toHaveText('Suburbs beyond 50 km');
      await page.locator('#export-close').click();
      await expect(page.locator('#export-panel')).toBeHidden();
      await expect(page.locator('#export-toggle')).toBeFocused();
      await page.unroute(memberUrl);
      await page.locator('#retry-data').click();
      await ready(page);
      for (const id of exportIds) await expect(page.locator(`#${id}`)).toBeEnabled();
      await openExport(page);
      await page.keyboard.press('Escape');
      await expect(page.locator('#export-panel')).toBeHidden();
      await expect(page.locator('#export-toggle')).toHaveAttribute('aria-expanded', 'false');
      await expect(page.locator('#export-toggle')).toBeFocused();
      await openExport(page);
      await page.locator('header h1').click();
      await expect(page.locator('#export-panel')).toBeHidden();
      await expect(page.locator('#export-toggle')).toHaveAttribute('aria-expanded', 'false');
      for (const prefix of ['map-controls', 'display-filters']) {
        await page.locator(`#${prefix}-toggle`).click();
        await expect(page.locator(`#${prefix}-panel`)).toBeVisible();
        await openExport(page);
        await expect(page.locator(`#${prefix}-panel`)).toBeHidden();
        await page.locator(`#${prefix}-toggle`).click();
        await expect(page.locator(`#${prefix}-panel`)).toBeVisible();
        await expect(page.locator('#export-panel')).toBeHidden();
        await expect(page.locator('#export-toggle')).toHaveAttribute('aria-expanded', 'false');
        await page.keyboard.press('Escape');
      }
      await expectBandReport(page);
      await page.locator('#analysis-controls input[value="group"]').check();
      await page.locator('#group-select').selectOption({ label: 'Central' });
      await expectBandReport(page, central.id);
      await expectHeaderFits(page);
    });

    test('suburb downloads match public road data for nearest, Central, slider changes and filtered maps', async ({ page }) => {
      await page.goto('/');
      await ready(page);
      await expectHeaderFits(page);
      await expect(page.locator('#analysis-controls input[value="nearest"]')).toBeChecked();
      await expect(page.locator('#support-distance')).toHaveValue('50');
      expect(new Set(expectedAssignments().map(row => row.groupName)).size,
        'Nearest fixture exercises actual assignments to multiple groups').toBeGreaterThan(1);
      await expectSuburbReports(page, 50);
      await filterFirstBand(page);
      await page.locator('#support-distance').evaluate(input => {
        input.value = '100';
        input.dispatchEvent(new Event('input', { bubbles: true }));
      });
      await expect(page.locator('#support-distance-value')).toHaveText('100 km');
      await expect(page.locator('#export-within')).toHaveText('Suburbs within 100 km');
      await expect(page.locator('#export-beyond')).toHaveText('Suburbs beyond 100 km');
      await expect(page.locator('#band-rows .band-button').first()).toHaveAttribute('aria-pressed', 'true');
      await expectSuburbReports(page, 100);
      await page.locator('#analysis-controls input[value="group"]').check();
      await page.locator('#group-select').selectOption({ label: 'Central' });
      await expect(page.locator('#group-select')).toHaveValue(central.id);
      await expect(page.locator('#export-context')).toHaveText('Central');
      await filterFirstBand(page);
      await expectSuburbReports(page, 100, central.id);
      await expectHeaderFits(page);
    });
  });
}
