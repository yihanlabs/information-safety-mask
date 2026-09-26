import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';
const python = existsSync('../.venv-cpu/Scripts/python.exe') ? '..\\.venv-cpu\\Scripts\\python.exe' : '..\\.venv\\Scripts\\python.exe';
export default defineConfig({
  testDir: './e2e', timeout: 60000, expect: { timeout: 10000 }, workers: 1,
  use: { baseURL: 'http://127.0.0.1:8765', channel: 'msedge', viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure' },
  outputDir: '../test-results/browser',
  webServer: { command: `${python} ..\\scripts\\ui_test_server.py`, url: 'http://127.0.0.1:8765', reuseExistingServer: false, timeout: 30000 },
});
