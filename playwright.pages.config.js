import { defineConfig } from '@playwright/test';
import browserConfig from './playwright.config.js';

const baseURL = 'http://127.0.0.1:4174/Ileostomy-association-member-heatmap-v2/';

export default defineConfig({
  testDir: './tests',
  testMatch: '**/pages.spec.mjs',
  timeout: 45_000,
  fullyParallel: true,
  workers: 2,
  outputDir: 'test-results-pages',
  reporter: [['line'], ['json', { outputFile: 'test-results-pages/browser-report.json' }]],
  use: {
    ...browserConfig.use,
    baseURL,
    launchOptions: browserConfig.use.launchOptions,
  },
  webServer: {
    command: 'npm run preview -- --port 4174',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 15_000,
  },
});
