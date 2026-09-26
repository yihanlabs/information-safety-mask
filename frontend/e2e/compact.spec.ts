import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

test('compact layout keeps canvas height and exposes persistent performance settings', async ({ page, request }) => {
  const headers = { 'X-Session-Token': 'e2e-local-only' };
  await request.delete('/api/images', { headers });
  await request.delete('/api/settings', { headers });
  await request.put('/api/performance', { headers, data: { mode: 'low_impact' } });
  const fixture = fs.readFileSync(path.resolve('../test-results/synthetic.png'));
  await request.post('/api/images', { headers, data: fixture });
  let images: any[] = [];
  await expect.poll(async () => {
    images = await (await request.get('/api/images', { headers })).json();
    return images[0]?.status;
  }).toBe('review');
  await request.post('/api/images/'+images[0].id+'/confirm', { headers, data: { revision: images[0].revision, reviewed: true } });
  images = await (await request.get('/api/images', { headers })).json();
  let processing = true;
  await page.route('**/api/images', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ json: processing ? [{ ...images[0], status: 'processing', confirmed: false,
      progress: { stage: 'recognizing', label: '识别图片文字', step: 4, steps: 5, elapsed_seconds: 120,
        waiting_seconds: 0, completed: 123, total: 320, percent: 38, eta_min_seconds: 20,
        eta_max_seconds: 80, eta_scope: 'image', estimate_basis: 'current', overdue: false } }] : images });
  });
  await page.goto('/#token=e2e-local-only');
  await expect(page.locator('.canvas-loading')).toHaveCount(0);
  const report: unknown[] = [];
  for (const size of [{ width: 1366, height: 768 }, { width: 1920, height: 1080 }]) {
    await page.setViewportSize(size);
    await expect(page.locator('.processing-card')).toContainText('已完成 123 / 320 行');
    await expect.poll(async () => (await page.getByTestId('canvas').boundingBox())!.height).toBeGreaterThanOrEqual(size.height*.8);
    const box = (await page.getByTestId('canvas').boundingBox())!;
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator('.processing-card').evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
    report.push({ ...size, canvasHeight: box.height, ratio: box.height/size.height });
    await page.screenshot({ path: `../test-results/compact-${size.width}.png` });
  }
  fs.writeFileSync('../test-results/compact-layout.json', JSON.stringify(report, null, 2));
  processing = false;
  await expect(page.locator('.processing-card')).toHaveCount(0);
  const height = (await page.getByTestId('canvas').boundingBox())!.height;
  await page.route('**/api/exports', route => route.fulfill({ status: 503, json: { detail: '合成下载失败，请重试' } }));
  await page.getByRole('button', { name: /下载已确认图片/ }).click();
  await expect(page.locator('.download-panel')).toContainText('合成下载失败');
  expect((await page.getByTestId('canvas').boundingBox())!.height).toBe(height);
  await page.getByRole('button', { name: '关闭下载详情', exact: true }).click();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const toggle = page.getByRole('switch', { name: '高占用模式', exact: true });
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  await expect(page.getByRole('dialog')).toContainText('当前使用高占用模式');
  const high = await (await request.get('/api/performance', { headers })).json();
  expect(high.active_mode).toBe('high_performance'); expect(high.cpu_budget_percent).toBe(80);
  expect((await page.getByTestId('canvas').boundingBox())!.height).toBe(height);
  await page.getByText('运行详情', { exact: true }).click();
  await page.screenshot({ path: '../test-results/performance-settings.png' });
  await page.getByRole('button', { name: '关闭设置', exact: true }).click();
  await page.reload();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect(page.getByRole('dialog')).toContainText('当前使用低占用模式');
  await page.getByRole('button', { name: '关闭设置', exact: true }).click();
  await page.getByLabel('自定义敏感内容', { exact: true }).fill('尚未添加的字段');
  await page.getByRole('button', { name: '收起规则与遮盖', exact: true }).click();
  await page.getByRole('button', { name: '展开规则与遮盖', exact: true }).click();
  await expect(page.getByLabel('自定义敏感内容', { exact: true })).toHaveValue('尚未添加的字段');
  const beforeError = (await page.getByTestId('canvas').boundingBox())!.height;
  await page.getByLabel('选择图片文件', { exact: true }).setInputFiles({ name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('broken') });
  await expect(page.getByRole('alert')).toContainText('broken.png');
  expect((await page.getByTestId('canvas').boundingBox())!.height).toBe(beforeError);
});
