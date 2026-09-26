import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

for (const previewReady of [true, false]) test(`memory waiting explains headroom and retries with preview ready=${previewReady}`, async ({ page, request }) => {
  const headers = { 'X-Session-Token': 'e2e-local-only' };
  await request.delete('/api/images', { headers });
  await request.post('/api/images', { headers, data: fs.readFileSync(path.resolve('../test-results/synthetic.png')) });
  let images: any[] = [];
  await expect.poll(async () => {
    images = await (await request.get('/api/images', { headers })).json();
    return images[0]?.status;
  }).toBe('review');
  const original = images[0];
  let waiting = true, retries = 0;
  await page.route('**/api/images', route => route.request().method() !== 'GET' ? route.continue() : route.fulfill({ json: [{ ...original,
    preview_ready: waiting ? previewReady : true,
    status: waiting ? 'waiting_memory' : 'review', message: waiting ? '加载文字模型所需内存不足；当前可用 2.0 GiB，预计本阶段需 3.5 GiB；建议再释放约 1.5 GiB 后重试' : '识别完成，请检查遮盖后确认',
    resource_issue: waiting ? { resource: 'memory', code: 'memory_preflight', stage: 'loading', required_bytes: 3.5*1024**3 } : null }] }));
  await page.route('**/api/health', async route => {
    const data = await (await route.fetch()).json();
    await route.fulfill({ json: { ...data, memory: { mode: 'saving', available_bytes: 2*1024**3, commit_available_bytes: 8*1024**3, waiting_image_id: waiting ? original.id : null, message: '已启用省内存处理，可能需要更长时间' } } });
  });
  await page.route(`**/api/images/${original.id}/retry`, route => { retries++; waiting = false; return route.fulfill({ json: original }); });
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto('/#token=e2e-local-only');
  await expect(page.locator('.review-alert')).toContainText('建议再释放约 1.5 GiB');
  await expect(page.locator('.queue-meta')).toContainText('等待释放内存');
  await expect(page.getByRole('button', { name: '确认此图片' })).toBeEnabled({ enabled: previewReady });
  const height = (await page.getByTestId('canvas').boundingBox())!.height;
  await page.getByText('释放内存', { exact: true }).click();
  await expect(page.locator('.memory-help')).toContainText('只关闭浏览器标签页不会释放后台模型');
  expect((await page.getByTestId('canvas').boundingBox())!.height).toBe(height);
  if (previewReady) await expect(page.getByText('正在载入图片', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: `../test-results/memory-waiting${previewReady ? '' : '-preparing'}.png` });
  await page.getByRole('button', { name: '重试处理' }).click();
  await expect(page.locator('.review-alert')).toHaveCount(0);
  expect(retries).toBe(1);
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByText('运行详情', { exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('已启用省内存处理');
});
