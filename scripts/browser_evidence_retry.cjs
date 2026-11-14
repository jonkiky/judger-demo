const {chromium}=require('../frontend/node_modules/@playwright/test');
const fs=require('node:fs');const path=require('node:path');const root=path.resolve(__dirname,'..');
(async()=>{
 const record=JSON.parse(fs.readFileSync(path.join(root,'evaluation/evidence-live-run.json')));
 const cred=JSON.parse(fs.readFileSync(path.join(root,'data/local-credentials.json')));
 const browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage();
 await page.goto('http://127.0.0.1:8765');await page.getByLabel('密码',{exact:true}).fill(cred.password);
 await page.getByRole('button',{name:'进入工作区'}).click();await page.getByRole('link',{name:'类案检索',exact:true}).waitFor();
 await page.goto('http://127.0.0.1:8765/cases/'+record.case_id+'/evidence');
 const response=page.waitForResponse(r=>r.url().endsWith('/runs/'+record.run_id+'/retry'));
 await page.getByRole('button',{name:'恢复未完成分析',exact:true}).click();const result=await response;
 if(result.status()!==202)throw new Error(await result.text());
 record.recovered_from_browser=true;fs.writeFileSync(path.join(root,'evaluation/evidence-live-run.json'),JSON.stringify(record,null,2));
 console.log(JSON.stringify({run_id:record.run_id,recovered_from_browser:true}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
