import { test, expect } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
const viewports = [
  { width: 1440, height: 1000 },
  { width: 1280, height: 650 },
  { width: 390, height: 844 },
  { width: 375, height: 500 },
];

test.beforeEach(async ({ page }) => {
  await page.route(/https:\/\/(tile\.openstreetmap\.org|services\.arcgisonline\.com)\/.+/, route =>
    route.fulfill({ status: 200, contentType: 'image/png', body: png }));
  // Capture the real map through the existing library setter, without app hooks.
  await page.addInitScript(() => {
    let library;
    Object.defineProperty(window, 'maplibregl', {
      configurable: true, get: () => library,
      set(value) {
        library = value;
        queueMicrotask(() => {
          value.Map = new Proxy(value.Map, {
            construct(Constructor, args) {
              const map = Reflect.construct(Constructor, args);
              window.__markerTestMap = map;
              return map;
            },
          });
        });
      },
    });
  });
});

async function expectViewportContained(page) {
  const metrics = await page.evaluate(() => ({
    width: innerWidth, height: innerHeight,
    documentWidth: document.documentElement.scrollWidth,
    documentHeight: document.documentElement.scrollHeight,
    bodyWidth: document.body.scrollWidth, bodyHeight: document.body.scrollHeight,
    bodyOverflow: getComputedStyle(document.body).overflow,
    bodyBoxHeight: document.body.getBoundingClientRect().height,
    scrollY,
  }));
  expect(metrics.documentWidth, 'No horizontal document overflow').toBeLessThanOrEqual(metrics.width + 1);
  expect(metrics.documentHeight, 'No vertical document overflow').toBeLessThanOrEqual(metrics.height + 1);
  expect(metrics.bodyWidth).toBeLessThanOrEqual(metrics.width + 1);
  expect(metrics.bodyHeight).toBeLessThanOrEqual(metrics.height + 1);
  expect(metrics.bodyOverflow).toBe('hidden');
  expect(Math.abs(metrics.bodyBoxHeight - metrics.height)).toBeLessThanOrEqual(1);
  expect(metrics.scrollY).toBe(0);
}

async function expectSelectionPlacement(page, mobile) {
  const card = page.locator('#selected-suburb');
  await expect(card).toBeVisible();
  if (mobile) {
    await expect(page.locator('#analysis-panel #selected-suburb-slot #selected-suburb')).toBeVisible();
  } else {
    await expect.poll(() => card.evaluate(el => el.closest('.map-stage') !== null),
      { message: 'Desktop selection returns to the map after responsive layout updates' }).toBe(true);
    expect(await card.evaluate(el => {
      const rect = el.getBoundingClientRect();
      const map = document.getElementById('map').getBoundingClientRect();
      return rect.left >= map.left - 1 && rect.right <= map.right + 1
        && rect.top >= map.top - 1 && rect.bottom <= map.bottom + 1;
    }), 'The complete selected card fits inside the map').toBe(true);
  }
}

async function expectRouteFits(page) {
  await expect.poll(() => page.evaluate(() => {
    const map = window.__markerTestMap;
    const geometry = map.getStyle().sources.route.data.features[0]?.geometry;
    if (!geometry) return false;
    const coordinates = geometry.type === 'MultiLineString' ? geometry.coordinates.flat() : geometry.coordinates;
    const { width, height } = map.getContainer().getBoundingClientRect();
    const points = coordinates.map(coordinate => map.project(coordinate));
    const padding = map.getPadding();
    const horizontalFill = (Math.max(...points.map(point => point.x)) - Math.min(...points.map(point => point.x)))
      / Math.max(1, width - padding.left - padding.right);
    const verticalFill = (Math.max(...points.map(point => point.y)) - Math.min(...points.map(point => point.y)))
      / Math.max(1, height - padding.top - padding.bottom);
    return !map.isMoving() && coordinates.length > 1 && map.getZoom() <= 17.001
      && Math.max(horizontalFill, verticalFill) > 0.3
      && points.every(point => point.x >= -1 && point.y >= -1 && point.x <= width + 1 && point.y <= height + 1);
  }), { message: 'The complete route fits this viewport at a useful scale' }).toBe(true);
}

for (const viewport of viewports) {
  test(`compact layout contains the map, toolbar, menus and sidebar at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/');
    await expect(page.locator('#joined-total')).toHaveText('3,019');
    await expect(page.locator('.group-marker')).toHaveCount(11);
    await expect(page.locator('#data-notice')).toBeHidden();
    await expectViewportContained(page);

    for (const id of ['map-controls-toggle', 'display-filters-toggle', 'theme-toggle']) {
      const button = page.locator(`header #${id}`);
      await expect(button).toBeInViewport({ ratio: 1 });
      expect(await button.evaluate(el => {
        const box = el.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth + 1;
      }), `${id} fits the responsive header`).toBe(true);
    }

    await page.locator('#suburb-search').fill('ADELAIDE');
    await expect(page.locator('#search-results')).toBeVisible();
    await expectViewportContained(page);
    await page.locator('#search-results').selectOption('suburb-20');
    await expect(page.locator('#route-status')).toContainText('Road route ready');
    await expect(page.locator('#fit-route')).toBeEnabled();
    await expectRouteFits(page);
    for (const direction of ['in', 'out']) {
      const before = await page.evaluate(() => window.__markerTestMap.getZoom());
      await page.locator(`.maplibregl-ctrl-zoom-${direction}`).click();
      await expect.poll(() => page.evaluate(() => window.__markerTestMap.getZoom()))
        [direction === 'in' ? 'toBeGreaterThan' : 'toBeLessThan'](before);
      await expect.poll(() => page.evaluate(() => window.__markerTestMap.isMoving())).toBe(false);
      await page.locator('#fit-route').click();
      await expectRouteFits(page);
    }
    await expectSelectionPlacement(page, viewport.width <= 850);
    await expect(page.locator('.maplibregl-popup')).toHaveCount(0);

    const toolbar = await page.evaluate(() => {
      const box = id => document.getElementById(id).getBoundingClientRect();
      const search = box('suburb-search'), clear = box('clear-search'), slider = box('support-distance');
      const label = id => {
        const el = document.querySelector(`label[for="${id}"]`);
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return { top: rect.top, bottom: rect.bottom, height: rect.height,
          lineHeight: parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2 };
      };
      return { centers: [search, clear, slider].map(rect => rect.top + rect.height / 2),
        ordered: search.right <= clear.left + 1 && clear.right <= slider.left + 1,
        within: [search, clear, slider].every(rect => rect.width > 0 && rect.left >= 0 && rect.right <= innerWidth + 1),
        labels: [label('suburb-search'), label('support-distance')] };
    });
    expect(Math.max(...toolbar.centers) - Math.min(...toolbar.centers), 'Search, Clear and slider share one row').toBeLessThanOrEqual(3);
    expect(toolbar.ordered).toBe(true);
    expect(toolbar.within).toBe(true);
    expect(Math.abs(toolbar.labels[0].top - toolbar.labels[1].top), 'Field labels align at the top').toBeLessThanOrEqual(3);
    expect(Math.abs(toolbar.labels[0].bottom - toolbar.labels[1].bottom), 'Field labels reserve the same row height').toBeLessThanOrEqual(3);
    for (const label of toolbar.labels) expect(label.height, 'Labels use at most two lines').toBeLessThanOrEqual(label.lineHeight * 2 + 1);

    const panes = await page.evaluate(() => {
      const map = document.querySelector('.map-section').getBoundingClientRect();
      const aside = document.getElementById('analysis-panel').getBoundingClientRect();
      return { map: { left: map.left, right: map.right, top: map.top, bottom: map.bottom, height: map.height },
        aside: { left: aside.left, right: aside.right, top: aside.top, bottom: aside.bottom, height: aside.height } };
    });
    if (viewport.width <= 850) {
      expect(panes.map.bottom).toBeLessThanOrEqual(panes.aside.top + 1);
      expect(Math.abs(panes.map.left - panes.aside.left)).toBeLessThanOrEqual(1);
      expect(Math.abs(panes.map.height / (panes.map.height + panes.aside.height) - 0.6)).toBeLessThan(0.08);
    } else {
      expect(panes.map.right).toBeLessThanOrEqual(panes.aside.left + 1);
      expect(Math.abs(panes.map.top - panes.aside.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(panes.map.bottom - panes.aside.bottom)).toBeLessThanOrEqual(1);
    }

    for (const prefix of ['map-controls', 'display-filters']) {
      await page.locator(`#${prefix}-toggle`).click();
      const panel = page.locator(`#${prefix}-panel`);
      await expect(panel).toBeVisible();
      await expect(panel).toHaveAttribute('popover', 'auto');
      const menu = await panel.evaluate(el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const scrollable = [el, ...el.querySelectorAll('*')].filter(node =>
          /auto|scroll/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 1);
        return { fits: rect.left >= -1 && rect.top >= -1 && rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1,
          maxHeight: style.maxHeight,
          scrolls: scrollable.map(node => { node.scrollTop = node.scrollHeight; return node.scrollTop > 0; }) };
      });
      expect(menu.fits, `${prefix} stays inside the viewport`).toBe(true);
      expect(menu.maxHeight, 'Menus have a viewport height limit').not.toBe('none');
      expect(menu.scrolls.every(Boolean), 'Overflowing menu content scrolls internally').toBe(true);
      if (prefix === 'display-filters') {
        await expect(panel.locator('#distance-min')).toHaveAttribute('type', 'number');
        await expect(panel.locator('#distance-max')).toHaveAttribute('type', 'number');
        const min = await panel.locator('#distance-min').boundingBox();
        const max = await panel.locator('#distance-max').boundingBox();
        expect(Math.abs(min.y + min.height / 2 - max.y - max.height / 2), 'Range fields align in one row').toBeLessThanOrEqual(3);
      }
      await expectViewportContained(page);
      await page.keyboard.press('Escape');
      await expect(panel).toBeHidden();
      await expect(page.locator(`#${prefix}-toggle`)).toBeFocused();
    }

    const sidebar = await page.locator('#analysis-panel').evaluate(el => {
      const overflows = el.scrollHeight > el.clientHeight + 1;
      const overflowY = getComputedStyle(el).overflowY;
      el.scrollTop = el.scrollHeight;
      const scrolled = el.scrollTop;
      el.scrollTop = 0;
      return { overflows, overflowY, scrolled };
    });
    expect(sidebar.overflowY).toMatch(/auto|scroll/);
    if (sidebar.overflows) expect(sidebar.scrolled, 'Sidebar scrolls independently of the document').toBeGreaterThan(0);
    await expectViewportContained(page);
    await expect(page.locator('#selected-meta, #selected-flags, #group-context')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText(/members in source\s*·.*mapped|Centroid to road:|Meeting point to road:|Select a cluster for its member and suburb breakdown/i);

    if (viewport.width === 1440) {
      await page.setViewportSize({ width: 390, height: 844 });
      await expectSelectionPlacement(page, true);
      await expectViewportContained(page);
      await page.setViewportSize(viewport);
      await expectSelectionPlacement(page, false);
      await expectViewportContained(page);
    }
  });
}
