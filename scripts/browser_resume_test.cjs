const {chromium}=require('../frontend/node_modules/@playwright/test');
const fs=require('node:fs');const path=require('node:path');const root=path.resolve(__dirname,'..');
(async()=>{
 const fixture=JSON.parse(fs.readFileSync(path.join(root,'evaluation/browser-upload.json')));
 const record=JSON.parse(fs.readFileSync(path.join(root,'evaluation/restart-recovery.json')));
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1080}});
 const credentials=JSON.parse(fs.readFileSync(path.join(root,'data/local-credentials.json')));
 await page.goto('http://127.0.0.1:8765');
 await page.getByLabel('密码',{exact:true}).fill(credentials.password);
 await page.getByRole('button',{name:'进入工作区'}).click();
 await page.getByRole('heading',{name:'案情有序，研究有据。'}).waitFor();
 await page.goto('http://127.0.0.1:8765/cases/'+fixture.case_id+'/research?run='+fixture.run_id);
 await page.getByText('等待您的选择',{exact:false}).first().waitFor();
 const restored=await (await page.request.get('http://127.0.0.1:8765/api/v1/runs/'+fixture.run_id)).json();
 record.after={id:restored.id,status:restored.status,revision:restored.revision};
 record.same_wait_payload=JSON.stringify(JSON.parse(record.before.wait_payload))===JSON.stringify(restored.wait_payload);
 if(!record.same_wait_payload||restored.status!=='waiting_input')throw new Error('Saved interrupt did not survive restart');
 await page.screenshot({path:path.join(root,'evaluation/screenshots/06-restored-wait.png'),fullPage:true});
 // A relevant but geographically different precedent, selected only in this evaluation fixture.
 for(const checkbox of await page.locator('.candidate').getByRole('checkbox').all())await checkbox.uncheck();
 await page.locator('.candidate').filter({hasText:'(2025)兵01民终194号'}).getByRole('checkbox').check();
 await page.getByRole('button',{name:'使用选定的 1 份案例继续'}).click();
 await page.getByRole('button',{name:'使用选定的 1 份案例继续'}).waitFor({state:'hidden'});
 record.resumed_via_browser=true;
 fs.writeFileSync(path.join(root,'evaluation/restart-recovery.json'),JSON.stringify(record,null,2));
 console.log(JSON.stringify({status:'resumed',same_wait_payload:record.same_wait_payload,run_id:fixture.run_id}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
