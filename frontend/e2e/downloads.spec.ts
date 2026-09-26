import { test, expect, type Download } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import type { ImageRecord } from '../src/types';

test('batch failure keeps successful PNGs and retries only the selected item', async ({ page, request }) => {
  const headers = { 'X-Session-Token': 'e2e-local-only' };
  await request.delete('/api/images', { headers });
  await request.delete('/api/settings', { headers });
  const fixture = fs.readFileSync(path.resolve('../test-results/synthetic.png'));
  for (let index = 0; index < 3; index++) {
    await request.post('/api/images', { headers: { ...headers, 'X-Image-Name': `sample-${index}.png` }, data: fixture });
  }
  let images: ImageRecord[] = [];
  await expect.poll(async () => {
    images = await (await request.get('/api/images', { headers })).json();
    return images.filter(item => item.status === 'review').length;
  }).toBe(3);
  for (const image of images) {
    await request.post(`/api/images/${image.id}/confirm`, { headers, data: { revision: image.revision, reviewed: true } });
  }
  let failSecond = true;
  const requested: { id: string; revision: number }[] = [];
  await page.route('**/api/exports', async route => {
    const body = route.request().postDataJSON();
    expect(body.format).toBe('png'); expect(body.images).toHaveLength(1);
    requested.push(body.images[0]);
    if (body.images[0].id === images[1].id && failSecond) {
      failSecond = false;
      return route.fulfill({ status: 503, json: { detail: '本张生成暂时失败，请重试' } });
    }
    return route.continue();
  });
  await page.route('**/api/health', route => route.fulfill({ json: {
    models: { ready: true, missing: [] }, settings_error: null, local_only: true,
    runtime: { mode: 'low_impact', backend: 'compatible', cpu_cap_applied: true, warning: null,
      fallback_reason: 'CPU 加速不兼容，已切换兼容模式' },
  } }));
  await page.goto('/#token=e2e-local-only');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('兼容模式');
  await page.getByRole('button', { name: '关闭设置', exact: true }).click();
  const downloaded: Download[] = [];
  page.on('download', item => downloaded.push(item));
  await page.getByRole('button', { name: /下载已确认图片/ }).click();
  await expect.poll(() => downloaded.length).toBe(2);
  await expect(page.locator('.download-panel')).toContainText('2 / 3 张已提交浏览器');
  await expect(page.locator('.download-error')).toContainText('本张生成暂时失败');
  await expect(page.locator('.download-panel')).toContainText('允许此地址下载多个文件');
  await page.getByRole('button', { name: `下载 sanitized_${String(images[1].number).padStart(3, '0')}.png`, exact: true }).click();
  await expect.poll(() => downloaded.length).toBe(3);
  await page.getByRole('button', { name: '下载详情', exact: true }).click();
  await expect(page.locator('.download-panel')).toContainText('3 / 3 张已提交浏览器');
  expect(requested).toEqual([...images, images[1]].map(({ id, revision }) => ({ id, revision })));
  expect(downloaded.every(item => item.suggestedFilename().endsWith('.png'))).toBe(true);

  // An edit from another tab invalidates the frozen revision used by the receipts.
  await request.put('/api/settings', { headers, data: { categories: [], custom_fields: [] } });
  await expect(page.locator('.download-panel li').getByRole('button').first()).toBeDisabled();
  await expect(page.locator('.download-panel')).toContainText('请重新确认后下载');
});
