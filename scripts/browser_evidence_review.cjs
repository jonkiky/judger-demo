// Exercise optional review through the real UI; model calls remain real.
const {chromium,expect:baseExpect}=require('../frontend/node_modules/@playwright/test');
const expect=baseExpect.configure({timeout:20000});
const fs=require('node:fs');const path=require('node:path');const crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
const read=name=>JSON.parse(fs.readFileSync(path.join(root,'evaluation',name)));
const save=(name,value)=>fs.writeFileSync(path.join(root,'evaluation',name),JSON.stringify(value,null,2));
const fingerprint=artifact=>crypto.createHash('sha256').update(JSON.stringify({body:artifact.body,structured:artifact.structured,sources:artifact.sources})).digest('hex');
(async()=>{
  const verify=process.argv.includes('--verify');
  const automatic=read('evidence-live-results.json');
  if(automatic.status!=='succeeded')throw new Error('Automatic run must finish first');
  const record=verify?read('evidence-review-run.json'):null;
  const credentials=JSON.parse(fs.readFileSync(path.join(root,'data/local-credentials.json')));
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1080}});const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8765');
    await page.getByLabel('密码',{exact:true}).fill(credentials.password);
    await page.getByRole('button',{name:'进入工作区'}).click();
    await page.getByRole('link',{name:'类案检索',exact:true}).waitFor();
    await expect.poll(async()=>{
      const health=await page.request.get('http://127.0.0.1:8765/api/v1/health');
      return health.ok()?(await health.json()).services?.search?.status:'unavailable';
    },{timeout:120000,intervals:[1000,2000,3000]}).toBe('ready');
    await page.goto('http://127.0.0.1:8765/cases/'+automatic.case_id+'/evidence');
    const region=page.getByRole('region',{name:'类案证据分析进度',exact:true});
    const currentId=verify?record.run_id:automatic.run_id;
    await expect(region).toHaveAttribute('data-run-id',currentId);
    await expect(region.locator('.evidence-agent')).toHaveCount(5);
    await expect(region.getByText('逐案响应 5 / 5',{exact:true})).toBeVisible();
    await expect(page.getByRole('heading',{name:'综合证据分析',exact:true})).toBeVisible();
    const get=async url=>{const response=await page.request.get('http://127.0.0.1:8765/api/v1'+url);if(!response.ok())throw new Error(await response.text());return response.json()};
    const original=await get('/artifacts/'+automatic.artifact_id);
    const originalHash=fingerprint(original);
    if(verify){
      const checked=read('evidence-review-results.json');
      if(checked.status!=='succeeded')throw new Error('Reviewed run must finish first');
      if(originalHash!==record.original_artifact_sha256)throw new Error('Reanalysis changed the original artifact');
      await expect(region.getByText('使用复核后选择的案例',{exact:true})).toBeVisible();
    }
    await region.locator('.evidence-agent-result').first().locator('summary').click();
    await region.locator('.evidence-agent-result').first().locator('button.citation').first().click();
    const source=page.getByRole('region',{name:'来源原文',exact:true});
    await expect(source).toBeVisible();
    await expect(source).toHaveAttribute('aria-busy','false');
    await expect(source.locator('.source-fulltext')).not.toBeEmpty();
    await page.getByRole('button',{name:'关闭来源',exact:true}).click();
    await expect(source).toHaveCount(0);
    const prefix=verify?'evidence-reviewed':'evidence-auto';
    await page.screenshot({path:path.join(root,'evaluation/screenshots',prefix+'-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await expect(region).toBeVisible();
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);
    if(overflow)throw new Error('Evidence page overflows mobile viewport');
    await page.screenshot({path:path.join(root,'evaluation/screenshots',prefix+'-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1080});
    const checks=['five_completed_agents_visible','synthesis_visible','individual_report_opens_exact_source','desktop_and_mobile_no_overflow'];
    if(verify){
      checks.push('reviewed_selection_visible','original_artifact_unchanged');
      if(errors.length)throw new Error(errors.join('\n'));
      save('evidence-browser-results.json',{automatic_run_id:automatic.run_id,reviewed_run_id:record.run_id,checks,page_errors:errors});
      console.log(JSON.stringify({checks,page_errors:errors}));return;
    }
    await page.getByRole('button',{name:'复核或调整类案',exact:true}).click();
    const review=page.getByRole('region',{name:'复核类案选择',exact:true});
    const options=review.getByRole('checkbox');
    const batch=automatic.progress.batch;
    await expect(options).toHaveCount(batch.candidates.length);
    await expect(review.getByText('已选 5 / 5',{exact:true})).toBeVisible();
    await expect(options.nth(5)).toBeDisabled();
    await options.nth(0).uncheck();
    await options.nth(5).check();
    await expect(review.getByText('已选 5 / 5',{exact:true})).toBeVisible();
    const selected=[...batch.selected_doc_ids.slice(1),batch.candidates[5].document_id];
    const accepted=page.waitForResponse(r=>r.url().endsWith('/requests')&&r.request().method()==='POST');
    await review.getByRole('button',{name:'使用选定类案重新分析',exact:true}).click();
    const response=await accepted;if(response.status()!==202)throw new Error(await response.text());
    const run=await response.json();const request=response.request().postDataJSON();
    expect(request.evidence_review).toEqual({run_id:automatic.run_id,doc_ids:selected});
    if(run.id===automatic.run_id)throw new Error('Review did not create a separate run');
    const next={case_id:automatic.case_id,run_id:run.id,source_run_id:automatic.run_id,selected_doc_ids:selected,
      original_artifact_id:automatic.artifact_id,original_artifact_sha256:originalHash,
      started_from_browser:true,checks:[...checks,'selection_limited_to_five','changed_case_selection_submitted'],page_errors:errors};
    save('evidence-review-run.json',next);console.log(JSON.stringify(next));
    await expect(region).toHaveAttribute('data-run-id',run.id);
    await expect(region.locator('.evidence-agent')).toHaveCount(5,{timeout:180000});
    await expect(page.getByText('上一轮成果 · 当前轮尚未完成',{exact:true})).toBeVisible();
    const after=await get('/artifacts/'+automatic.artifact_id);
    if(fingerprint(after)!==originalHash)throw new Error('Reanalysis changed the original artifact');
    if(errors.length)throw new Error(errors.join('\n'));
    next.checks.push('original_result_preserved_during_reanalysis');save('evidence-review-run.json',next);
  }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
