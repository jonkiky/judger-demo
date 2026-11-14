// Real local API/browser; the acceptance case is fictional and separate.
const {chromium,expect:baseExpect}=require('../frontend/node_modules/@playwright/test');
const fs=require('node:fs');const path=require('node:path');
const expect=baseExpect.configure({timeout:20000});
const root=path.resolve(__dirname,'..');
const record=JSON.parse(fs.readFileSync(path.join(root,'evaluation/research-live-run.json')));
const credentials=JSON.parse(fs.readFileSync(record.credentials_path||path.join(root,'data/local-credentials.json')));
const base=record.base||'http://127.0.0.1:8765';
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const errors=[];const results={};
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base);
  await page.getByLabel('密码',{exact:true}).fill(credentials.password);
  await page.getByRole('button',{name:'进入工作区'}).click();
  await page.getByRole('link',{name:'类案检索',exact:true}).waitFor();
  if(!process.argv.includes('--chat')&&!process.argv.includes('--selection')){
   const existing=await (await page.request.get(base+'/api/v1/cases/'+record.case_id)).json();
   if(existing.title!=='研究功能验收 临时虚构案例')throw new Error('Only clean up the dedicated acceptance fixture');
   const me=await (await page.request.get(base+'/api/v1/me')).json();
   for(const doc of existing.reference_documents||[]){
    const response=await page.request.delete(base+'/api/v1/reference-documents/'+doc.id,{headers:{'X-CSRF-Token':me.csrf}});
    if(!response.ok())throw new Error('Could not clean previous test document');
   }
  }
  await page.goto(base+'/cases/'+record.case_id+'/research?run='+record.run_id);
  if(process.argv.includes('--selection')){
   await page.getByRole('button',{name:'检索与选案',exact:true}).click();
   await page.getByLabel('类案检索问题').fill('房屋买卖 逾期交房');
   await page.getByRole('button',{name:'检索类案',exact:true}).click();
   await page.locator('.search-result').first().waitFor({timeout:90000});
   const inputs=page.locator('.search-result input[type=checkbox]');
   await expect(inputs.first()).toBeVisible();
   await expect(page.locator('.research-selected')).toHaveCount(record.doc_ids.length);
   await page.getByRole('button',{name:'清空选择',exact:true}).click();
   for(let i=0;i<5;i++)await inputs.nth(i).check();
   await expect(inputs.nth(5)).toBeDisabled();
   results.limitAtFive=true;
   await inputs.nth(0).uncheck();
   await expect(inputs.nth(5)).toBeEnabled();
   results.canChangeSelection=true;
   await page.screenshot({path:path.join(root,'evaluation/screenshots/research-selection.png'),fullPage:true});
  }else{
   await expect(page.getByLabel('选择研究版本')).toBeVisible();
   const progress=page.getByRole('region',{name:'类案研究进度',exact:true});
   await expect(progress.locator('.evidence-agent')).toHaveCount(record.doc_ids.length);
   await expect(progress.getByText(`已完成 ${record.doc_ids.length} / ${record.doc_ids.length} 个类案`,{exact:true})).toBeVisible();
   await progress.locator('summary').first().click();
   await progress.locator('.citation').first().click();
   await expect(page.getByRole('region',{name:'来源原文',exact:true})).toBeVisible();
   await page.getByRole('button',{name:'关闭来源',exact:true}).click();
   results.selectedCasesAndSources=record.doc_ids.length;
   if(process.argv.includes('--chat')){
    await page.getByLabel('研究消息',{exact:true}).fill('请将下一步整理为核对材料、核实通知到达和比较主位与备位路径的具体行动顺序。');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.getByText('正在结合本轮来源回答…',{exact:true})).toBeVisible();
    results.chatSubmitted=true;
   }else{
    await expect(page.locator('.research-chat-turn')).toHaveCount(1);
    const direct=page.waitForEvent('download');
    await page.getByRole('button',{name:'下载 Word 供审阅',exact:true}).click();
    const download=await direct;await download.saveAs(path.join(root,'evaluation/research-refined-review.docx'));
    await page.getByRole('button',{name:'保存 Word 到材料中心',exact:true}).click();
    await expect(page.getByText('当前研究报告与追问记录已保存到材料中心。',{exact:true})).toBeVisible();
    await page.getByRole('link',{name:'查看材料中心',exact:true}).click();
    const docs=page.locator('.reference-documents');
    await expect(docs.locator('.reference-document')).toHaveCount(1);
    const savedDownload=page.waitForEvent('download');
    await docs.getByRole('link',{name:'下载 Word',exact:true}).click();
    const saved=await savedDownload;
    if(!saved.suggestedFilename().endsWith('.docx'))throw new Error('Expected DOCX');
    await page.screenshot({path:path.join(root,'evaluation/screenshots/research-materials.png'),fullPage:true});
    await docs.getByRole('button',{name:'删除文档',exact:true}).click();
    await docs.getByRole('button',{name:'确认删除文档',exact:true}).click();
    await expect(docs.locator('.reference-document')).toHaveCount(0);
    await page.reload();
    await expect(page.locator('.reference-document')).toHaveCount(0);
    await page.goto(base+'/cases/'+record.case_id+'/research');
    await expect(page.locator('.research-chat-turn')).toHaveCount(1);
    await page.getByRole('region',{name:'研究对话',exact:true}).screenshot({path:path.join(root,'evaluation/screenshots/research-chat.png')});
    results.downloadSaveDeleteAndRetainedChat=true;
   }
   await page.screenshot({path:path.join(root,'evaluation/screenshots/research-desktop.png'),fullPage:true});
  }
  await page.setViewportSize({width:390,height:844});
  await page.goto(base+'/cases/'+record.case_id+'/research');
  await page.getByRole('heading',{name:'结果与研究',exact:true}).waitFor();
  results.mobileNoOverflow=await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1);
  if(!results.mobileNoOverflow)throw new Error('Mobile horizontal overflow');
  await page.screenshot({path:path.join(root,'evaluation/screenshots/research-mobile.png'),fullPage:true});
  if(errors.length)throw new Error(errors.join('\n'));
  results.consoleErrors=errors;
  fs.writeFileSync(path.join(root,'evaluation/research-browser'+(process.argv.includes('--selection')?'-selection':process.argv.includes('--chat')?'-chat':'')+'.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
