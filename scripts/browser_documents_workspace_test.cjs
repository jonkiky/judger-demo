// Browser + real API/storage; deterministic model responses exercise the actual graph.
const {chromium,expect:baseExpect}=require('../frontend/node_modules/@playwright/test');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),net=require('node:net');
const {spawn,execFileSync}=require('node:child_process');
const {once}=require('node:events');
const expect=baseExpect.configure({timeout:12000});
const root=path.resolve(__dirname,'..');

async function main(){
  const data=fs.mkdtempSync(path.join(os.tmpdir(),'caiyan-document-browser-'));
  fs.mkdirSync(path.join(data,'corpus'));
  fs.writeFileSync(path.join(data,'corpus/release.json'),JSON.stringify({release_id:'document-ui-fixture'}));
  const env={...process.env,CAIYAN_DATA_DIR:data,LITELLM_LOCAL_MODEL_COST_MAP:'True'};
  const socket=net.createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');
  const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));
  const base=`http://127.0.0.1:${port}`;
  const server=spawn(path.join(root,'.venv/bin/python'),['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port',String(port),'--no-access-log','--timeout-graceful-shutdown','2'],{cwd:root,env,stdio:['ignore','pipe','pipe']});
  let logs='',browser;
  server.stdout.on('data',x=>logs+=x);server.stderr.on('data',x=>logs+=x);
  function complete(rid){
    execFileSync(path.join(root,'.venv/bin/python'),['-c',`
import sys,json,time
from langgraph.checkpoint.memory import InMemorySaver
from backend.app import document_agents as da, workflows as w
from backend.app.db import connect,dumps,now
rid=sys.argv[1]
with connect() as c:r=dict(c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())
def generate(op,system,payload,*args,**kwargs):
    if ':document_read:' in op:
        return {'findings':[{'section':section,'summary':payload['spans'][0]['quote'],'source_ids':[payload['spans'][0]['id']]} for section in payload['required_sections']], 'missing':[]}
    return {'paragraphs':[{'text':s['quote'],'source_ids':[s['id']]} for s in payload['sources']], 'unresolved':['核对材料原件。']}
da.generate=generate
state={'run_id':rid,'snapshot':json.loads(r['snapshot']),'plan':json.loads(r['plan']),'started':time.time()}
w.build_graph(InMemorySaver()).invoke(state,{'configurable':{'thread_id':rid}})
with connect(True) as c:c.execute("UPDATE runs SET status='succeeded',stage='文书已保存',updated_at=? WHERE id=?",(now(),rid))
`,rid],{cwd:root,env,stdio:['ignore','pipe','pipe']});
  }
  try{
    for(let i=0;;i++){if(i>150||server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break}catch{}await new Promise(r=>setTimeout(r,100))}
    browser=await chromium.launch({channel:'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/api/v1/health',route=>route.fulfill({json:{corpus:null,services:{}}}));
    await page.goto(base+'/cases');
    await page.getByLabel('密码',{exact:true}).fill(JSON.parse(fs.readFileSync(path.join(data,'local-credentials.json'))).password);
    await page.getByRole('button',{name:'进入工作区'}).click();
    await expect(page.getByRole('heading',{name:'我的案件',exact:true})).toBeVisible();
    const me=await (await page.request.get(base+'/api/v1/me')).json();
    const request=async(method,url,body)=>{const response=await page.request.fetch(base+'/api/v1'+url,{method,data:body,headers:{'X-CSRF-Token':me.csrf,'Idempotency-Key':crypto.randomUUID()}});if(!response.ok())throw Error(await response.text());return response.json()};
    const c=await request('POST','/cases',{title:'文书交互验收（虚构）',claims:'核对日期和付款'});
    await request('POST','/cases/'+c.id+'/materials/text',{name:'合同摘要',text:'双方于2026年1月1日签订合同，约定2026年5月31日交房。'});
    await request('POST','/cases/'+c.id+'/materials/text',{name:'付款记录',text:'买方于2026年1月2日支付十万元房款。'});
    const url=base+'/cases/'+c.id;
    await page.goto(url+'/documents');
    await expect(page.getByRole('heading',{name:'文书草稿',exact:true})).toBeVisible();
    await page.getByLabel('上传文书模板').setInputFiles({name:'诉状模板.md',mimeType:'text/markdown',buffer:Buffer.from('# 诉状\n\n## 事实经过\n填写事实。\n\n## 诉讼请求\n填写请求。')});
    await expect(page.getByLabel('文书模板',{exact:true})).not.toHaveValue('builtin:complaint');
    await expect(page.locator('.document-material')).toHaveCount(2);
    await page.getByLabel('阅读重点：付款记录').fill('查找付款金额及日期');
    await page.getByLabel('文书起草要求').fill('按给定模板起草，缺少的法院名称写待补充。');
    const submit=page.waitForResponse(r=>r.url().endsWith('/requests')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'起草文书',exact:true}).click();
    const rid=(await (await submit).json()).id;
    await expect(page).toHaveURL(new RegExp('/documents\\?run='+rid));
    const frozen=await request('GET','/runs/'+rid);
    expect(frozen.plan).toEqual(['document.generate']);
    expect(Object.values(frozen.snapshot.document_tasks)).toContain('查找付款金额及日期');
    await page.reload();
    await expect(page.getByLabel('文书生成进展')).toContainText('按给定模板起草');
    complete(rid);
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    await expect(page.getByRole('heading',{name:'审阅文书',exact:true})).toBeVisible();
    await expect(page.getByText('Word 已保存到材料中心',{exact:true})).toBeVisible();
    await expect(page.getByLabel('文书生成进展').locator('details')).toHaveCount(2);
    await page.locator('.report .citation').first().click();
    await expect(page.getByLabel('来源全文')).toBeVisible();
    await expect(page.getByLabel('来源全文').locator('mark')).toBeVisible();
    await page.getByRole('button',{name:'关闭来源'}).click();
    await page.getByRole('button',{name:'编辑正文',exact:true}).click();
    const editor=page.getByLabel('编辑文书正文');
    const current=await editor.inputValue();await editor.fill(current+'\n\n人工补充：需核对原件。');
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    await expect(editor).toHaveValue(current+'\n\n人工补充：需核对原件。');
    await page.getByRole('button',{name:'保存新版本',exact:true}).click();
    await expect(page.getByLabel('选择文书版本').locator('option:checked')).toContainText('v2');
    await expect(page.locator('.report')).toContainText('人工补充：需核对原件。');
    await page.getByLabel('文书修改要求').fill('保留付款记录，将事实按时间顺序整理。');
    const revised=page.waitForResponse(r=>r.url().endsWith('/requests')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'按要求修改文书',exact:true}).click();
    const next=(await (await revised).json()).id;
    const snapshot=await request('GET','/runs/'+next);
    expect(snapshot.snapshot.document_context.body).toContain('人工补充：需核对原件。');
    complete(next);await page.getByRole('button',{name:'刷新',exact:true}).click();
    await expect(page.getByLabel('选择文书版本').locator('option:checked')).toContainText('v3');
    const downloaded=page.waitForEvent('download');
    await page.getByRole('button',{name:'下载 Word 草稿',exact:true}).click();
    expect((await downloaded).suggestedFilename()).toMatch(/\.docx$/);
    await page.goto(url+'/materials');
    await expect(page.getByRole('heading',{name:'文书与研究文档',exact:true})).toBeVisible();
    await expect(page.getByRole('link',{name:'查看与修改文书',exact:true})).toHaveCount(3);
    await page.getByRole('link',{name:'查看与修改文书',exact:true}).first().click();
    await expect(page).toHaveURL(/\/documents\?artifact=/);
    await expect(page.getByLabel('选择文书版本').locator('option:checked')).toContainText('v3');
    const shots=path.join(root,'evaluation/document-workspace');fs.mkdirSync(shots,{recursive:true});
    await page.screenshot({path:path.join(shots,'desktop.png'),fullPage:true});
    await page.goto(url+'/tasks');
    await expect(page.locator('textarea,input:not([type=hidden])')).toHaveCount(0);
    await expect(page.getByRole('link',{name:'打开文书草稿',exact:true})).toHaveCount(2);
    await page.getByRole('link',{name:'打开文书草稿',exact:true}).first().click();
    await expect(page).toHaveURL(/\/documents\?run=/);
    await page.setViewportSize({width:390,height:844});
    await expect(page.getByRole('button',{name:'起草文书',exact:true})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
    await page.screenshot({path:path.join(shots,'mobile.png'),fullPage:true});
    expect(errors).toEqual([]);
    const summary={passed:true,checks:['template upload','per-material instructions','generation stays in documents','refresh persists run','all reader reports','exact source highlight','manual edit survives polling','immutable Word versions','instruction revision uses edited body','download before review','materials links','read-only task log','mobile layout'],consoleErrors:errors};
    fs.writeFileSync(path.join(shots,'browser-result.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary));
  }finally{if(browser)await browser.close();server.kill('SIGTERM');await once(server,'exit')}
}
main().catch(e=>{console.error(e);process.exitCode=1});
