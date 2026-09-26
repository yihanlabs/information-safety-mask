// Record the real local workbench using synthetic fixtures and actual OCR.
// No request interception, mocked model responses, or user browser profiles.
const { chromium, expect } = require('../../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const out = path.join(root, 'test-results/promo-v0.1.1');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const session = JSON.parse(fs.readFileSync(path.join(out, 'session.json'), 'utf8'));
  const base = `http://127.0.0.1:${session.port}`;
  const headers = { 'X-Session-Token': session.token };
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 960 }, deviceScaleFactor: 1,
    recordVideo: { dir: path.join(out, 'raw'), size: { width: 1920, height: 960 } } });
  const started = Date.now();
  const page = await context.newPage();
  // A recording-only pointer indicator; it never intercepts input or changes app state.
  await page.addInitScript(() => {
    window.addEventListener('DOMContentLoaded', () => {
      const pointer = document.createElement('span');
      pointer.style.cssText = 'position:fixed;left:-40px;top:-40px;width:16px;height:16px;margin:-8px;border-radius:50%;background:#176d57b0;border:2px solid white;box-shadow:0 0 0 1px #176d57;z-index:999999;pointer-events:none';
      document.body.append(pointer);
      window.addEventListener('mousemove',e=>{pointer.style.left=e.clientX+'px';pointer.style.top=e.clientY+'px'});
      window.addEventListener('mousedown',()=>{pointer.style.transform='scale(1.6)'});
      window.addEventListener('mouseup',()=>{pointer.style.transform='scale(1)'});
    });
  });
  const marks = {};
  const mark = key => { marks[key] = (Date.now() - started) / 1000; };
  const errors = [];
  const downloads = [];
  page.on('pageerror', err => errors.push(err.message));
  page.on('download', item => downloads.push(item));
  const clean = async () => {
    await expect(page.locator('.canvas-loading')).toHaveCount(0, { timeout: 30000 });
    await expect(page.locator('.toast')).toHaveCount(0, { timeout: 10000 });
    await expect(page.locator('.banner.error,.detail-retry')).toHaveCount(0);
  };
  let records;
  try {
    await page.goto(`${base}/#token=${session.token}`);
    await expect(page.getByRole('button', { name: '导入图片', exact: true })).toBeVisible();
    mark('import_start');
    await sleep(500);
    await page.getByLabel('选择图片文件', { exact: true }).setInputFiles([
      { name: '示例登记表.png', mimeType: 'image/png', buffer: fs.readFileSync(path.join(out, 'form.png')) },
      { name: '长图检查示例.png', mimeType: 'image/png', buffer: fs.readFileSync(path.join(out, 'long.png')) },
    ]);
    await sleep(2400);
    mark('import_end');
    const deadline = Date.now() + 20 * 60 * 1000;
    while (true) {
      records = await (await context.request.get(`${base}/api/images`, { headers })).json();
      if (records.some(r => ['error', 'waiting_memory'].includes(r.status))) throw Error('Demo processing did not complete');
      if (records.length === 2 && records.every(r => r.status === 'review')) break;
      if (Date.now() > deadline) throw Error('Real OCR exceeded demonstration deadline');
      console.log(JSON.stringify(records.map(r => ({ status: r.status, stage: r.progress?.stage, done: r.progress?.completed }))));
      await sleep(10000);
    }
    await page.getByRole('button', { name: '查看 示例登记表.png', exact: true }).click();
    await page.getByRole('button', { name: '查看全图', exact: true }).click();
    await clean();
    mark('automatic_start');
    await page.mouse.move(1460, 810, { steps: 18 });
    await sleep(4500);
    await page.screenshot({ path: path.join(out, 'workbench.png') });
    mark('automatic_end');
    await page.getByRole('button', { name: '确认并下一张', exact: true }).click();
    await expect(page.locator('.active-filename')).toHaveText('长图检查示例.png');
    await page.getByRole('button', { name: '适应宽度', exact: true }).click();
    await clean();
    mark('long_start');
    const canvas = page.getByTestId('canvas');
    const bounds = await canvas.boundingBox();
    // Review exposed edge pixels just as a user would: expand this auto mask in the UI.
    const longRecord = records.find(r=>r.name==='长图检查示例.png');
    const phone = longRecord.masks.find(m=>m.reasons.some(reason=>reason.includes('电话号码')));
    if (!phone) throw Error('Expected synthetic phone mask was not detected');
    await page.getByRole('tab',{name:/当前遮盖/}).click();
    await page.locator('.mask-info').filter({hasText:'电话号码'}).click();
    await clean(); await sleep(350);
    const adjust = { x:Number(await canvas.getAttribute('data-view-x')), y:Number(await canvas.getAttribute('data-view-y')), scale:Number(await canvas.getAttribute('data-scale')) };
    const corner={x:bounds.x+adjust.x+phone.box.x*adjust.scale,y:bounds.y+adjust.y+phone.box.y*adjust.scale};
    await page.mouse.move(corner.x,corner.y,{steps:16}); await page.mouse.down();
    await page.mouse.move(corner.x-10*adjust.scale,corner.y-10*adjust.scale,{steps:22}); await page.mouse.up();
    await expect.poll(async()=>{
      const current=await (await context.request.get(`${base}/api/images`,{headers})).json();
      return current.find(r=>r.id===longRecord.id).edits.overrides[phone.id]?.x ?? phone.box.x;
    }).toBeLessThan(phone.box.x-5);
    await sleep(400);
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    const scrollAmount=Number(await canvas.getAttribute('data-view-y'))-(16-1320*Number(await canvas.getAttribute('data-scale')));
    for (let i = 0; i < 6; i++) { await page.mouse.wheel(0,scrollAmount/6); await sleep(230); }
    await clean();
    await sleep(400);
    await page.getByRole('button', { name: '手动补框', exact: true }).click();
    const view = { x: Number(await canvas.getAttribute('data-view-x')), y: Number(await canvas.getAttribute('data-view-y')), scale: Number(await canvas.getAttribute('data-scale')) };
    const p = (x,y) => ({ x: bounds.x + view.x + x*view.scale, y: bounds.y + view.y + y*view.scale });
    const a=p(219,1733), b=p(479,1772);
    await page.mouse.move(a.x,a.y,{steps:18}); await sleep(350); await page.mouse.down();
    await page.mouse.move(b.x,b.y,{steps:24}); await page.mouse.up();
    await page.getByRole('button', { name: '选择与调整', exact: true }).click();
    await page.getByRole('tab', { name: /当前遮盖/ }).click();
    await page.locator('.mask-info').filter({hasText:'手动遮盖'}).last().click();
    await expect(page.locator('.mask-item.chosen')).toContainText('手动遮盖');
    await clean(); await sleep(1400);
    await page.screenshot({ path: path.join(out, 'manual-mask.png') });
    mark('long_end');
    mark('export_start');
    await page.getByRole('button',{name:'确认此图片',exact:true}).click();
    await expect(page.getByRole('button',{name:/下载已确认图片/})).toBeEnabled();
    await sleep(600);
    await page.getByRole('button',{name:/下载已确认图片/}).click();
    await expect.poll(()=>downloads.length,{timeout:60000}).toBe(2);
    for (const item of downloads) {
      await item.saveAs(path.join(out,item.suggestedFilename()));
      if (await item.failure()) throw Error('Demo PNG download failed');
    }
    await page.getByRole('button',{name:'下载详情',exact:true}).click();
    await expect(page.locator('.download-panel')).toContainText('2 / 2 张已提交浏览器');
    await sleep(2200);
    mark('export_end');
    if (errors.length) throw Error(errors.join('\n'));
    records=await (await context.request.get(`${base}/api/images`,{headers})).json();
    fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify({ records,downloaded:downloads.map(x=>x.suggestedFilename()),runtime:await (await context.request.get(`${base}/api/health`,{headers})).json() },null,2));
    fs.writeFileSync(path.join(out,'marks.json'),JSON.stringify(marks,null,2));
    console.log('Real OCR, manual mask and two PNG downloads completed.');
  } finally {
    const rawPath=await page.video().path();
    await context.close();
    fs.copyFileSync(rawPath,path.join(out,'raw.webm'));
    await browser.close();
    // Only this independently created demo session has this random token.
    await fetch(`${base}/api/demo-stop`,{method:'POST',headers}).catch(()=>{});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
