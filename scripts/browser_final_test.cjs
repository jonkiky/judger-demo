const {chromium}=require('../frontend/node_modules/@playwright/test');
const fs=require('node:fs');const path=require('node:path');const crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
(async()=>{
 const x=JSON.parse(fs.readFileSync(path.join(root,'evaluation/acceptance-runs.json')));
 const credentials=JSON.parse(fs.readFileSync(path.join(root,'data/local-credentials.json')));
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1080},acceptDownloads:true});const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8765');
 await page.getByLabel('密码',{exact:true}).fill(credentials.password);
 await page.getByRole('button',{name:'进入工作区'}).click();
 await page.getByRole('heading',{name:'案情有序，研究有据。'}).waitFor();
 const records=[];
 for(const [key,label] of [['complaint','民事起诉状'],['opinion','代理意见']]){
  if(!x[key+'_artifact'])continue;
  await page.goto('http://127.0.0.1:8765/cases/'+x.case_id+'/documents');
  await page.getByLabel('选择文书版本').selectOption(x[key+'_artifact']);
  await page.locator('.report .citation').first().click();
  await page.getByRole('region',{name:'来源原文'}).waitFor();
  await page.locator('.source-fulltext').waitFor();
  await page.screenshot({path:path.join(root,'evaluation/screenshots/'+key+'-source.png')});
  await page.getByRole('button',{name:'关闭来源'}).click();
  const exportButton=page.getByRole('button',{name:'导出 Markdown'});
  const wasLocked=!(await exportButton.isEnabled());
  if(wasLocked){
   await page.getByLabel('我已复核当前版本及其来源').check();
   await page.getByRole('button',{name:'记录复核'}).click();
   await exportButton.waitFor();
   await page.waitForFunction(()=>Array.from(document.querySelectorAll('button')).some(b=>b.textContent.includes('导出 Markdown')&&!b.disabled));
  }
  await page.screenshot({path:path.join(root,'evaluation/screenshots/'+key+'-reviewed.png')});
  const downloaded=page.waitForEvent('download');await exportButton.click();const download=await downloaded;
  const target=path.join(root,'evaluation/reviewed/'+label+'_含来源.md');await download.saveAs(target);
  const content=fs.readFileSync(target,'utf8');
  if(!content.includes('来源与版本附录')||!content.includes(label))throw new Error('Export missing provenance or title');
  records.push({kind:key,artifact_id:x[key+'_artifact'],required_review:wasLocked,source_inline:true,downloaded:true,bytes:Buffer.byteLength(content),sha256:crypto.createHash('sha256').update(content).digest('hex')});
 }
 await page.goto('http://127.0.0.1:8765/settings');await page.getByRole('heading',{name:'写作配置',exact:true}).waitFor();
 await page.screenshot({path:path.join(root,'evaluation/screenshots/09-config-history.png'),fullPage:true});
 await page.goto('http://127.0.0.1:8765/cases/'+x.case_id+'/documents');
 await page.getByLabel('选择文书版本').waitFor();await page.setViewportSize({width:390,height:844});
 await page.locator('#workspace-navigation').waitFor({state:'hidden'});
 await page.getByRole('button',{name:'菜单',exact:true}).click();
 await page.locator('#workspace-navigation').waitFor({state:'visible'});
 await page.getByRole('link',{name:/我的案件/}).first().click();
 await page.locator('#workspace-navigation').waitFor({state:'hidden'});
 await page.goto('http://127.0.0.1:8765/cases/'+x.case_id+'/documents');
 await page.getByLabel('选择文书版本').waitFor();
 await page.screenshot({path:path.join(root,'evaluation/screenshots/10-documents-mobile.png')});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
 if(errors.length||overflow)throw new Error(JSON.stringify({errors,overflow}));
 fs.writeFileSync(path.join(root,'evaluation/browser-final.json'),JSON.stringify({records,errors,mobile_navigation_verified:true,mobile_horizontal_overflow:overflow},null,2));
 console.log(JSON.stringify({records,errors,mobile_navigation_verified:true,mobile_horizontal_overflow:overflow}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
