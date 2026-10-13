import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  retries: 0,
  timeout: 60000,
  use: { baseURL: 'http://127.0.0.1:8748', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: '.venv/bin/python -m tests.fakes.web_server',
    cwd: '..',
    url: 'http://127.0.0.1:8748/api/sessions',
    reuseExistingServer: false,
    timeout: 30000,
  },
  reporter: [['list'], ['html', { open: 'never' }]],
});
