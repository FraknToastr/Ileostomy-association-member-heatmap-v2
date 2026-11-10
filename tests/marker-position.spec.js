import { test, expect } from '@playwright/test';

test('support-group dots stay on their projected coordinates through zoom, pan and resize', async ({ page }) => {
  // Capture the actual map without adding a debug API to the application.
  await page.addInitScript(() => {
    let library;
    Object.defineProperty(window, 'maplibregl', {
      configurable: true,
      get: () => library,
      set(value) {
        library = value;
        queueMicrotask(() => {
          value.Map = new Proxy(value.Map, {
            construct(MapConstructor, args) {
              const map = Reflect.construct(MapConstructor, args);
              window.__markerTestMap = map;
              return map;
            },
          });
        });
      },
    });
  });
  await page.goto('/');
  await expect(page.locator('.group-marker')).toHaveCount(11);
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    for (const zoom of [8, 6, 4, 3]) {
      const errors = await page.evaluate(async ({ zoom }) => {
        const map = window.__markerTestMap;
        map.resize();
        map.jumpTo({ center: zoom % 2 ? [138.6, -34.9] : [136.8, -32.5], zoom });
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const { groups } = await (await fetch('./public/data/analysis.json')).json();
        const container = map.getContainer().getBoundingClientRect();
        const markers = [...document.querySelectorAll('.group-marker')];
        return groups.map(group => {
          const marker = markers.find(el => el.querySelector('.group-name').textContent === group.name);
          const dot = marker.querySelector('.group-dot').getBoundingClientRect();
          const expected = map.project(group.coordinates);
          return { group: group.name, position: getComputedStyle(marker).position,
            error: Math.hypot(dot.left + dot.width / 2 - container.left - expected.x,
              dot.top + dot.height / 2 - container.top - expected.y) };
        });
      }, { zoom });
      for (const marker of errors) {
        expect(marker.position, `${marker.group} at zoom ${zoom}`).toBe('absolute');
        expect(marker.error, `${marker.group} at zoom ${zoom}, viewport ${viewport.width}`).toBeLessThan(1.5);
      }
    }
  }
});
