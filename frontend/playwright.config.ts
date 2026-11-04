import { defineConfig, devices } from '@playwright/test';

// CI uses the Vite default port. Locally, set PLAYWRIGHT_PORT when 3000 is
// taken (the dev server and the tests both follow it).
const PORT = process.env.PLAYWRIGHT_PORT || '3000';
const BASE_URL = `http://localhost:${PORT}`;

// The journey suite (e2e/journeys) drives the real backend and the mock
// provider instead of route mocks; it runs only when JOURNEYS=1 and the
// backend is up (see e2e/journeys/README.md and the CI job).
const JOURNEYS = process.env.JOURNEYS === '1';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',

  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: JOURNEYS
    ? [
        {
          name: 'journeys',
          testMatch: /journeys\/.*\.spec\.ts$/,
          use: { ...devices['Desktop Chrome'] },
        },
      ]
    : [
        {
          name: 'chromium',
          testIgnore: /journeys\//,
          use: { ...devices['Desktop Chrome'] },
        },
        {
          name: 'firefox',
          testIgnore: /journeys\//,
          use: { ...devices['Desktop Firefox'] },
        },
      ],

  webServer: {
    command: `npm run dev -- --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
