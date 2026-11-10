import { defineConfig } from '@playwright/test';
import { createRequire } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const registry = JSON.parse(readFileSync(path.join(path.dirname(require.resolve('playwright-core/package.json')), 'browsers.json'), 'utf8'));
const revision = registry.browsers.find(b => b.name === 'chromium-headless-shell').revision;
// This host sets PLAYWRIGHT_BROWSERS_PATH=/usr/share/playwright. Prefer only a
// matching revision in the writable user cache if that system location is old.
const candidates = [process.env.PLAYWRIGHT_BROWSERS_PATH, path.join(os.homedir(), '.cache/ms-playwright')]
  .filter(Boolean).map(root => path.join(root, `chromium_headless_shell-${revision}`, 'chrome-headless-shell-linux64/chrome-headless-shell'));
const executablePath = process.platform === 'linux' ? candidates.find(existsSync) : undefined;

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.js',
  timeout: 30_000,
  fullyParallel: true,
  workers: 2,
  reporter: [['line'], ['json', { outputFile: 'test-results/browser-report.json' }]],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
    launchOptions: {
      ...(executablePath ? { executablePath } : {}),
      args: ['--enable-webgl', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
    },
  },
  webServer: {
    command: 'node scripts/dev-server.mjs',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 15_000,
  },
});
