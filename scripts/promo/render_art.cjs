const { chromium }=require('../../frontend/node_modules/@playwright/test');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
(async()=>{
 const root=path.resolve(__dirname,'../..');
 const out=path.join(root,'test-results/promo-v0.1.1');
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage();
  const url=pathToFileURL(path.join(__dirname,'artwork.html')).href;
  for(const scene of ['social','intro','outro']){
   await page.setViewportSize(scene==='social'?{width:1280,height:640}:{width:1920,height:960});
   await page.goto(url+(scene==='social'?'':'?scene='+scene));
   await page.evaluate(async()=>{await document.fonts.ready;await Promise.all([...document.images].map(i=>i.decode()));});
   await page.screenshot({path:scene==='social'?path.join(root,'docs/images/social-preview.png'):path.join(out,scene+'.png'),omitBackground:false});
  }
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
