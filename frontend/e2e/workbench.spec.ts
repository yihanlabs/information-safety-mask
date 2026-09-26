import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import type { ImageRecord, ProcessingProgress } from '../src/types';

test.beforeEach(async ({ request }) => {
  const headers={'X-Session-Token':'e2e-local-only'};
  await request.delete('/api/images',{headers});
  await request.delete('/api/settings',{headers});
});

test('actual stage counts, queue, refresh and connection loss remain honest', async ({ page, request }) => {
  const headers = { 'X-Session-Token': 'e2e-local-only' };
  const fixture = fs.readFileSync(path.resolve('../test-results/synthetic.png'));
  await request.post('/api/images', { headers: { ...headers, 'X-Image-Name': 'progress.png' }, data: fixture });
  await request.post('/api/images', { headers: { ...headers, 'X-Image-Name': 'queued.png' }, data: fixture });
  await expect.poll(async () => {
    const images: ImageRecord[] = await (await request.get('/api/images', { headers })).json();
    return images.filter(image => image.status === 'review').length;
  }).toBe(2);
  const originals: ImageRecord[] = await (await request.get('/api/images', { headers })).json();
  let progress: ProcessingProgress = {
    stage: 'detecting', label: '寻找文字位置', step: 2, steps: 4, elapsed_seconds: 95, waiting_seconds: 0,
    completed: 0, total: 1, percent: null, eta_min_seconds: 20, eta_max_seconds: 80,
    eta_scope: 'stage', estimate_basis: 'rough', overdue: false,
  };
  let offline = false, done = false;
  await page.route('**/api/images', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    if (offline) return route.abort('failed');
    return route.fulfill({ json: done ? originals : [
      { ...originals[0], status: 'processing', message: progress.label, masks: [], confirmed: false, progress },
      { ...originals[1], status: 'queued', message: '等待识别', masks: [], queue_position: 1, progress: { ...progress, stage: 'queued', waiting_seconds: 95 } },
    ] });
  });
  await page.goto('/#token=e2e-local-only');
  const card = page.locator('.processing-card');
  const bar = page.getByRole('progressbar', { name: '当前阶段进度' });
  await expect(card).toContainText('寻找文字位置');
  await expect(card).toContainText('01:35');
  await expect(card).toContainText('本阶段还需');
  await expect(bar).not.toHaveAttribute('aria-valuenow');
  await expect(page.getByRole('button', { name: '确认并下一张', exact: true })).toBeDisabled();
  await page.screenshot({ path: '../test-results/workbench-progress-detection.png' });

  progress = { ...progress, unit: 'tile', completed: 1, total: 3, percent: 33 };
  await expect(bar).toHaveAttribute('aria-valuenow', '33');
  await expect(card).toContainText('已完成 1 / 3 块');

  progress = { ...progress, unit: null, stage: 'recognizing', label: '识别图片文字', step: 3, completed: 30, total: 120,
    percent: 25, eta_scope: 'image', estimate_basis: 'current' };
  await expect(bar).toHaveAttribute('aria-valuenow', '25');
  await expect(card).toContainText('已完成 30 / 120 行');
  await page.reload();
  await expect(bar).toHaveAttribute('aria-valuenow', '25');
  await page.screenshot({ path: '../test-results/workbench-progress-counts.png' });
  await page.getByRole('button', { name: '查看 queued.png', exact: true }).click();
  await expect(card).toContainText('排队第 1 张');
  await expect(card).not.toContainText('还需');
  await page.getByRole('button', { name: '查看 progress.png', exact: true }).click();

  offline = true;
  await expect(card).toContainText('进度暂时无法更新');
  await expect(card).not.toContainText('还需');
  offline = false;
  progress = { ...progress, overdue: true, eta_min_seconds: null, eta_max_seconds: null };
  await expect(card).toContainText('已超出估计时间');
  await expect(bar).toHaveAttribute('aria-valuenow', '25');
  done = true;
  await expect(card).toHaveCount(0);
  await expect(page.getByRole('button', { name: '确认并下一张', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: '下载已确认图片', exact: true })).toBeDisabled();
});

test('batch review, independent rules, mask edits and confirmed export', async ({ page, request }) => {
  const errors: string[] = [], external: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!request.url().startsWith('http://127.0.0.1:8765') && !request.url().startsWith('blob:') && !request.url().startsWith('data:')) external.push(request.url()); });
  await page.goto('/#token=e2e-local-only');
  await expect(page.getByText('拖入图片，开始脱敏')).toBeVisible();
  await expect(page.getByRole('checkbox', { name: '人名', exact: true })).toBeChecked();
  await expect(page.getByRole('button', { name: '下载已确认图片' })).toBeDisabled();
  await page.screenshot({ path: '../test-results/workbench-empty.png' });
  const fixture=fs.readFileSync(path.resolve('../test-results/synthetic.png'));
  await page.getByLabel('选择图片文件', { exact: true }).setInputFiles([
    {name:'示例登记表.png',mimeType:'image/png',buffer:fixture},
    {name:'第二张.png',mimeType:'image/png',buffer:fixture},
  ]);
  await expect(page.locator('.queue-item')).toHaveCount(2);
  await expect(page.getByRole('button',{name:'确认并下一张',exact:true})).toBeEnabled();
  await expect(page.locator('.mask-item')).toHaveCount(4);
  await page.getByRole('tab', { name: /当前遮盖/ }).click();
  await page.getByRole('button',{name:'取消遮盖 1',exact:true}).click();
  await expect(page.locator('.mask-item')).toHaveCount(3);
  await page.getByRole('button',{name:'撤销',exact:true}).click();
  await expect(page.locator('.mask-item')).toHaveCount(4);
  await page.getByRole('button',{name:'重做',exact:true}).click();
  await expect(page.locator('.mask-item')).toHaveCount(3);
  await page.getByRole('button',{name:/恢复自动遮盖/}).click();
  await expect(page.locator('.mask-item')).toHaveCount(4);
  await page.getByRole('tab', { name: '批次规则', exact: true }).click();
  await page.getByLabel('自定义敏感内容',{exact:true}).fill('ALPHA-42\n不在图片中的内容');
  await page.getByRole('button',{name:'添加到字段列表'}).click();
  await expect(page.locator('.custom-row')).toHaveCount(2);
  await expect(page.locator('.mask-item')).toHaveCount(5);
  await page.getByRole('checkbox',{name:'ALPHA-42',exact:true}).uncheck();
  await expect(page.locator('.mask-item')).toHaveCount(4);
  await page.getByRole('checkbox',{name:'ALPHA-42',exact:true}).check();
  await expect(page.locator('.mask-item')).toHaveCount(5);

  const headers={'X-Session-Token':'e2e-local-only'};
  let images=await (await request.get('/api/images',{headers})).json();
  const first=images[0];
  const canvas=(await page.getByTestId('canvas').boundingBox())!;
  const scale=Number(await page.getByTestId('canvas').getAttribute('data-scale'));
  const origin={x:canvas.x+Number(await page.getByTestId('canvas').getAttribute('data-view-x')),y:canvas.y+Number(await page.getByTestId('canvas').getAttribute('data-view-y'))};
  const original=first.masks[0].box;
  await page.getByRole('tab', { name: /当前遮盖/ }).click();
  await page.locator('.mask-info').first().click();
  const center={x:origin.x+(original.x+original.width/2)*scale,y:origin.y+(original.y+original.height/2)*scale};
  await page.mouse.move(center.x,center.y);
  await page.mouse.down(); await page.mouse.move(center.x+30,center.y+10,{steps:8}); await page.mouse.up();
  await expect.poll(async()=> (await (await request.get('/api/images',{headers})).json())[0].edits.overrides[first.masks[0].id]?.x).toBeGreaterThan(original.x+10);
  await page.getByRole('button',{name:'撤销',exact:true}).click();
  await expect(page.getByRole('button',{name:'确认并下一张',exact:true})).toBeEnabled();
  await page.locator('.mask-info').first().click();
  const corner={x:origin.x+(original.x+original.width)*scale,y:origin.y+(original.y+original.height)*scale};
  await page.mouse.move(corner.x,corner.y);
  await page.mouse.down(); await page.mouse.move(corner.x+24,corner.y+18,{steps:8}); await page.mouse.up();
  await expect.poll(async()=> (await (await request.get('/api/images',{headers})).json())[0].edits.overrides[first.masks[0].id]?.width).toBeGreaterThan(original.width+10);
  await page.getByRole('button',{name:'撤销',exact:true}).click();
  await expect(page.getByRole('button',{name:'确认并下一张',exact:true})).toBeEnabled();

  await page.getByRole('button',{name:'手动补框',exact:true}).click();
  await page.mouse.move(origin.x+650*scale,origin.y+510*scale);
  await page.mouse.down(); await page.mouse.move(origin.x+850*scale,origin.y+560*scale,{steps:8}); await page.mouse.up();
  await expect(page.locator('.mask-item')).toHaveCount(6);
  await page.getByRole('button',{name:'选择与调整',exact:true}).click();
  await page.screenshot({path:'../test-results/workbench-review.png'});
  await page.getByRole('button',{name:'确认并下一张',exact:true}).click();
  await expect(page.getByRole('button',{name:'确认此图片',exact:true})).toBeEnabled();
  await expect(page.getByRole('button',{name:/下载已确认图片/})).toBeEnabled();
  await page.getByRole('tab', { name: '批次规则', exact: true }).click();
  await page.getByRole('checkbox',{name:'电话号码',exact:true}).uncheck();
  await expect(page.getByRole('button',{name:'下载已确认图片'})).toBeDisabled();
  await page.getByRole('button',{name:'查看 示例登记表.png',exact:true}).click();
  await page.getByRole('tab', { name: /当前遮盖/ }).click();
  await expect(page.getByText('手动遮盖',{exact:true}).first()).toBeVisible();
  await page.getByRole('button',{name:'确认并下一张',exact:true}).click();
  await page.getByRole('button',{name:'确认此图片',exact:true}).click();
  const downloads: import('@playwright/test').Download[] = [];
  page.on('download', download => downloads.push(download));
  await page.getByRole('button',{name:/下载已确认图片/}).click();
  await expect.poll(() => downloads.length).toBe(2);
  expect(downloads.map(item => item.suggestedFilename())).toEqual(images.map((item: ImageRecord) => `sanitized_${String(item.number).padStart(3, '0')}.png`));
  for (const item of downloads) {
    const filename = '../test-results/browser-' + item.suggestedFilename();
    await item.saveAs(filename);
    expect(fs.readFileSync(filename).subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
  }
  await page.getByRole('button', { name: '下载详情', exact: true }).click();
  await expect(page.locator('.download-panel')).toContainText('2 / 2 张已提交浏览器');
  await page.screenshot({path:'../test-results/workbench-png-downloads.png'});
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
});


test('field editing, reload persistence, manual mode and partial import failures', async ({ page }) => {
  await page.goto('/#token=e2e-local-only');
  await page.getByLabel('自定义敏感内容',{exact:true}).fill('甲公司\n乙公司');
  await page.getByRole('button',{name:'添加到字段列表'}).click();
  await expect(page.locator('.custom-row')).toHaveCount(2);
  await page.getByRole('button',{name:'编辑 甲公司',exact:true}).click();
  await page.getByLabel('编辑字段内容').fill('甲集团');
  await page.getByRole('button',{name:'保存字段',exact:true}).click();
  await expect(page.getByRole('checkbox',{name:'甲集团',exact:true})).toBeChecked();
  await page.getByRole('checkbox',{name:'乙公司',exact:true}).uncheck();
  await expect(page.getByRole('checkbox',{name:'乙公司',exact:true})).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('checkbox',{name:'甲集团',exact:true})).toBeChecked();
  await expect(page.getByRole('checkbox',{name:'乙公司',exact:true})).not.toBeChecked();
  for (const name of ['人名','证件号码','详细住址','电话号码']) {
    await page.getByRole('checkbox',{name,exact:true}).uncheck();
    await expect(page.getByRole('checkbox',{name,exact:true})).toBeEnabled();
  }
  await page.getByRole('button',{name:'清空字段',exact:true}).click();
  await expect(page.getByText('当前为手动遮盖模式')).toBeVisible();
  const fixture=fs.readFileSync(path.resolve('../test-results/synthetic.png'));
  await page.getByLabel('选择图片文件',{exact:true}).setInputFiles([
    {name:'good.png',mimeType:'image/png',buffer:fixture},
    {name:'broken.png',mimeType:'image/png',buffer:Buffer.from('not a png')},
  ]);
  await expect(page.locator('.queue-item')).toHaveCount(1);
  await expect(page.getByRole('alert')).toContainText('broken.png');
  await expect(page.getByRole('button',{name:'确认此图片',exact:true})).toBeEnabled();
  await expect(page.locator('.mask-item')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'导出本张',exact:true})).toBeDisabled();
});
