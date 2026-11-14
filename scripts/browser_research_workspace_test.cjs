// Browser + real API on isolated storage. Worker states and source text are fixtures;
// requests, resume validation, recovery, cancellation and history use the real API.
const {chromium, expect: baseExpect} = require('../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const expect = baseExpect.configure({timeout: 10000});
const root = path.resolve(__dirname, '..');

async function main() {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'caiyan-research-workspace-'));
  fs.mkdirSync(path.join(data, 'corpus'));
  fs.writeFileSync(path.join(data, 'corpus/release.json'), JSON.stringify({release_id:'research-ui-fixture',scope:'isolated UI test'}));
  const env = {...process.env, CAIYAN_DATA_DIR:data, LITELLM_LOCAL_MODEL_COST_MAP:'True'};
  const socket = net.createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(root, '.venv/bin/python'), ['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port',String(port),'--no-access-log','--timeout-graceful-shutdown','5'], {
    cwd:root, env, stdio:['ignore','pipe','pipe'],
  });
  let logs='';
  server.stdout.on('data', chunk => { logs+=chunk; });
  server.stderr.on('data', chunk => { logs+=chunk; });
  function workerState(id, fields, artifact=false) {
    execFileSync(path.join(root, '.venv/bin/python'), ['-c', `
import json, sys
from backend.app.db import connect, dumps, digest, now, event
args=json.load(sys.stdin)
fields=args['fields']
assert set(fields).issubset({'status','stage','wait_payload','resume_payload','error','revision','plan'})
with connect(True) as c:
    for key,value in fields.items():
        if isinstance(value,(dict,list)): value=dumps(value)
        c.execute('UPDATE runs SET '+key+'=? WHERE id=?',(value,args['id']))
    event(c,args['id'],'fixture.progress',{'stage':fields.get('stage','测试阶段更新')})
    if args['artifact']:
        r=c.execute('SELECT * FROM runs WHERE id=?',(args['id'],)).fetchone()
        body='# 类案研究验收成果\\n\\n这是隔离测试成果，用于验证研究完成后在当前页面展示。'
        c.execute('INSERT INTO artifacts (id,case_id,run_id,kind,version,title,body,structured,sources,content_hash,checks,completeness,case_revision,created_at,operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          ('research-ui-artifact',r['case_id'],r['id'],'research',1,'类案研究验收成果',body,dumps({'sections':[{'heading':'研究回复','paragraphs':[{'text':'这是测试研究回复。','source_ids':[]}]}],'conversation':[]}),'[]',digest(body),dumps({'passed':True}),'limited',r['revision'],now(),'research-ui-complete'))
`], {cwd:root, env, input:JSON.stringify({id,fields,artifact})});
  }
  let browser;
  try {
    for(let attempt=0;;attempt++) {
      if(server.exitCode!==null||attempt>150)throw new Error('Test API did not start: '+logs);
      try {if((await fetch(base+'/healthz')).ok)break;}catch{}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    browser=await chromium.launch({channel:'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1080}});
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.route('**/api/v1/health', route=>route.fulfill({json:{corpus:null,services:{}}}));
    await page.goto(base+'/cases');
    const credentials=JSON.parse(fs.readFileSync(path.join(data,'local-credentials.json'),'utf8'));
    await page.getByLabel('密码',{exact:true}).fill(credentials.password);
    await page.getByRole('button',{name:'进入工作区'}).click();
    await expect(page.getByRole('heading',{name:'我的案件',exact:true})).toBeVisible();
    const me=await (await page.request.get(base+'/api/v1/me')).json();
    const request=async(method,endpoint,body)=>{
      const response=await page.request.fetch(base+'/api/v1'+endpoint,{method,data:body,
        headers:{'X-CSRF-Token':me.csrf,'Idempotency-Key':crypto.randomUUID()}});
      if(!response.ok())throw new Error(`${method} ${endpoint}: ${response.status()} ${await response.text()}`);
      return response.json();
    };
    const c=await request('POST','/cases',{title:'研究交互位置验收（虚构）',claims:'核查房屋交付争议的类案和处理路径'});
    const materials=[];
    for(const name of ['测试合同摘要','付款凭证','交付通知','往来函件'])materials.push(await request('POST','/cases/'+c.id+'/materials/text',{name,text:'双方约定交付房屋。实际履行情况及责任仍待核对。'}));
    const candidates=Array.from({length:5},(_,i)=>({document_id:'research-fixture-'+(i+1),
      title:'房屋交付争议测试案例 '+(i+1),case_number:`（测试）民终${i+1}号`,court:'虚构测试法院',quote:'这是一段用于界面验收的虚构裁判摘录。'}));
    execFileSync(path.join(root,'.venv/bin/python'),['-c',`
import json,sys
from backend.app.db import connect
from backend.app.contracts import SimilarCaseInput
from backend.app.similar_cases import save_similar
args=json.load(sys.stdin)
with connect(True) as c:
    revision=c.execute('SELECT source_revision FROM cases WHERE id=?',(args['cid'],)).fetchone()[0]
    save_similar(c,args['cid'],SimilarCaseInput(),response={'items':args['candidates'],'release_id':'research-ui-fixture'},source_revision=revision)
`],{cwd:root,env,input:JSON.stringify({cid:c.id,candidates})});
    const caseUrl=base+'/cases/'+c.id;
    const screenshots=path.join(root,'evaluation/screenshots');
    fs.mkdirSync(screenshots,{recursive:true});
    const runPanel=id=>page.locator(`.research-run[data-run-id="${id}"]`);
    const logCard=id=>page.locator('#run-'+id);
    const refresh=async()=>{
      const response=page.waitForResponse(r=>r.url()===base+'/api/v1/cases/'+c.id&&r.request().method()==='GET');
      await page.getByRole('button',{name:'刷新',exact:true}).click();
      await response;
    };

    // The empty log routes to the interaction workspace and has no composer.
    await page.goto(caseUrl+'/tasks');
    await expect(page.getByLabel('研究消息')).toHaveCount(0);
    await page.getByRole('link',{name:'前往结果与研究',exact:true}).click();
    const caseScope=page.getByRole('region',{name:'类案',exact:true});
    const materialScope=page.getByRole('region',{name:'本次材料范围',exact:true});
    await expect(caseScope.getByRole('checkbox').nth(3)).toBeDisabled();
    await expect(materialScope.getByRole('checkbox').nth(3)).toBeDisabled();
    await caseScope.getByRole('checkbox').first().uncheck();
    await caseScope.getByRole('checkbox').nth(3).check();
    await materialScope.getByRole('checkbox').first().uncheck();
    await materialScope.getByRole('checkbox').nth(3).check();
    await page.getByRole('button',{name:'风险与抗辩 检查不利事实与应对方案',exact:true}).click();
    await expect(page.getByLabel('研究消息')).toHaveValue(/本案不利事实/);
    await expect(page.getByLabel('案件研究目标')).toHaveCount(0);
    await expect(page.getByRole('textbox')).toHaveCount(1);
    const chatBox=await page.getByRole('region',{name:'研究对话',exact:true}).boundingBox();
    const processBox=await page.getByRole('complementary',{name:'研究过程与来源',exact:true}).boundingBox();
    expect(processBox.x).toBeGreaterThanOrEqual(chatBox.x+chatBox.width);
    await page.evaluate(()=>{window.scrollTo(0,0);document.querySelector('.research-sidebar').scrollTop=0});
    expect((await page.getByRole('button',{name:'发送',exact:true}).boundingBox()).y+40).toBeLessThanOrEqual(1080);
    await page.screenshot({path:path.join(screenshots,'research-chat-empty-desktop.png'),fullPage:true});
    // Resize the existing chat instead of remounting it: draft and source picks survive.
    const draft=await page.getByLabel('研究消息').inputValue();
    const chat=page.getByRole('region',{name:'研究对话',exact:true});
    expect((await page.locator('.topbar').boundingBox()).height).toBe(48);
    await page.getByRole('button',{name:'收起左侧导航',exact:true}).click();
    expect((await page.getByRole('complementary',{name:'工作区导航'}).boundingBox()).width).toBe(68);
    await page.getByRole('button',{name:'收起研究过程与来源',exact:true}).click();
    await expect(page.getByRole('complementary',{name:'研究过程与来源',exact:true})).toBeHidden();
    await expect(page.getByRole('button',{name:'展开研究侧栏',exact:true})).toBeVisible();
    expect((await chat.boundingBox()).width).toBeGreaterThan(chatBox.width+300);
    await expect(page.getByLabel('研究消息')).toHaveValue(draft);
    await page.screenshot({path:path.join(screenshots,'research-both-sidebars-collapsed.png'),fullPage:true});
    await page.getByRole('button',{name:'全屏聊天',exact:true}).click();
    const fullscreen=page.getByRole('region',{name:'全屏研究工作区',exact:true});
    await expect(fullscreen).toBeVisible();
    expect(await fullscreen.boundingBox()).toEqual({x:0,y:0,width:1440,height:1080});
    await expect(page.locator('.topbar')).toBeHidden();
    await expect(page.getByLabel('研究消息')).toHaveValue(draft);
    expect(await page.evaluate(()=>document.body.style.overflow)).toBe('hidden');
    await page.getByLabel('研究消息').dispatchEvent('keydown',{key:'Escape',isComposing:true,bubbles:true});
    await expect(fullscreen).toBeVisible();
    await page.screenshot({path:path.join(screenshots,'research-fullscreen-desktop.png'),fullPage:true});
    // Tab stays in the fullscreen surface; source reading remains usable above it.
    await fullscreen.focus();
    await page.keyboard.press('Shift+Tab');
    expect(await page.evaluate(()=>document.querySelector('.research-chat-workspace').contains(document.activeElement))).toBe(true);
    await page.getByRole('button',{name:'展开研究过程与来源',exact:true}).click();
    await expect(caseScope.getByRole('checkbox').nth(3)).toBeChecked();
    await expect(materialScope.getByRole('checkbox').nth(3)).toBeChecked();
    await page.getByRole('button',{name:'查看材料原文：付款凭证',exact:true}).click();
    await expect(page.getByRole('region',{name:'来源原文',exact:true})).toBeVisible();
    expect((await page.locator('.workspace-source').boundingBox()).y).toBe(12);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('region',{name:'来源原文',exact:true})).toHaveCount(0);
    await expect(fullscreen).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(fullscreen).toHaveCount(0);
    await expect(page.getByRole('button',{name:'全屏聊天',exact:true})).toBeFocused();
    await expect(page.getByRole('button',{name:'展开研究过程与来源',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'展开左侧导航',exact:true})).toBeVisible();
    expect(await page.evaluate(()=>document.body.style.overflow)).toBe('');
    await page.getByRole('button',{name:'展开研究过程与来源',exact:true}).click();
    await page.getByRole('button',{name:'展开左侧导航',exact:true}).click();
    await expect(page.getByLabel('研究消息')).toHaveValue(draft);
    await expect(caseScope.getByRole('checkbox').nth(3)).toBeChecked();
    // The same controls remain accessible on a phone, including the process drawer.
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button',{name:'全屏聊天',exact:true}).click();
    expect(await fullscreen.boundingBox()).toEqual({x:0,y:0,width:390,height:844});
    await expect(page.getByRole('button',{name:'退出全屏聊天',exact:true})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
    await page.screenshot({path:path.join(screenshots,'research-fullscreen-mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'展开研究过程与来源',exact:true}).click();
    const drawer=await page.getByRole('complementary',{name:'研究过程与来源',exact:true}).boundingBox();
    expect(drawer.x).toBeGreaterThan(0);expect(drawer.x+drawer.width).toBeLessThanOrEqual(390);
    await page.getByRole('button',{name:'收起研究侧栏',exact:true}).click();
    await page.getByRole('button',{name:'退出全屏聊天',exact:true}).click();
    await expect(page.getByLabel('研究消息')).toHaveValue(draft);
    await page.setViewportSize({width:1440,height:1080});
    await expect(caseScope.getByRole('checkbox').nth(3)).toBeChecked();
    await expect(materialScope.getByRole('checkbox').nth(3)).toBeChecked();

    const createdResponse=page.waitForResponse(r=>r.url().endsWith('/requests')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    const run=await (await createdResponse).json();
    const snapshot=(await request('GET','/runs/'+run.id)).snapshot;
    expect(snapshot.selected_doc_ids).toEqual(candidates.slice(1,4).map(item=>item.document_id));
    expect(snapshot.material_ids).toEqual(materials.slice(1,4).map(item=>item.id));
    await expect(page).toHaveURL(caseUrl+'/research?run='+run.id);
    await expect(runPanel(run.id)).toContainText('等待执行');
    await expect(page.locator('.research-message.user')).toContainText('本案不利事实');
    await expect(page.getByRole('button',{name:'发送',exact:true})).toBeDisabled();
    await page.reload();
    await expect(runPanel(run.id)).toBeVisible();
    const pinnedScope=page.getByRole('region',{name:'本轮研究依据'});
    await expect(pinnedScope.getByRole('button')).toHaveCount(6);
    await expect(pinnedScope).toContainText(candidates[3].title);
    await expect(pinnedScope).not.toContainText(candidates[0].title);

    const wait={kind:'case_selection',question:'请选择用于本轮研究的类案。',candidates:candidates.slice(0,3)};
    workerState(run.id,{status:'waiting_input',stage:'等待您的选择',wait_payload:wait});
    // Polling reveals the question without navigation or a reload.
    await expect(runPanel(run.id).getByRole('checkbox')).toHaveCount(3);
    await expect(page.locator('.tabs a.active')).toContainText('结果与研究');
    await expect(page.locator('.tabs a').filter({hasText:'结果与研究'}).locator('i')).toHaveCount(1);
    await expect(page.locator('.tabs a').filter({hasText:'任务记录'}).locator('i')).toHaveCount(0);
    for(const checkbox of await runPanel(run.id).getByRole('checkbox').all())await expect(checkbox).toBeChecked();
    for(const checkbox of await runPanel(run.id).getByRole('checkbox').all())await checkbox.uncheck();
    await runPanel(run.id).getByRole('checkbox').first().check();
    await page.getByLabel('研究消息').fill('下一轮研究草稿应在轮询和核对来源后保留。');
    await refresh();
    await expect(runPanel(run.id).getByRole('checkbox').first()).toBeChecked();
    await expect(page.getByLabel('研究消息')).toHaveValue('下一轮研究草稿应在轮询和核对来源后保留。');

    await page.route('**/api/v1/judgments/research-fixture-1', route=>route.fulfill({json:{...candidates[0],text:candidates[0].quote}}));
    await runPanel(run.id).getByRole('button',{name:'查看原文'}).first().click();
    await expect(page.getByRole('region',{name:'来源原文'})).toBeVisible();
    await expect(page).toHaveURL(caseUrl+'/research?run='+run.id);
    await page.getByRole('button',{name:'关闭来源'}).click();
    await expect(runPanel(run.id).getByRole('checkbox').first()).toBeChecked();
    await page.screenshot({path:path.join(screenshots,'research-interaction-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(async()=>{scrollTo(0,0);await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);});
    await page.screenshot({path:path.join(screenshots,'research-interaction-mobile.png'),fullPage:true});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);

    // Task history never renders research inputs, and points to the exact run.
    await runPanel(run.id).getByRole('link',{name:'查看执行记录'}).click();
    await expect(page).toHaveURL(caseUrl+'/tasks#run-'+run.id);
    await expect(page.getByLabel('研究消息')).toHaveCount(0);
    await expect(page.locator('.candidate, .waiting')).toHaveCount(0);
    await expect(page.locator('.tab-content input, .tab-content textarea')).toHaveCount(0);
    await expect(page.getByRole('button',{name:'取消任务',exact:true})).toHaveCount(0);
    await expect(logCard(run.id)).toContainText('选择类案并继续分析');
    await logCard(run.id).getByRole('button',{name:'执行详情'}).click();
    await expect(page.locator('.run-detail')).toContainText('等待您的选择');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
    await page.setViewportSize({width:1440,height:1080});
    await page.screenshot({path:path.join(screenshots,'research-task-history.png'),fullPage:true});
    await logCard(run.id).getByRole('link',{name:'前往结果与研究继续'}).click();
    await expect(page).toHaveURL(caseUrl+'/research?run='+run.id);
    await page.reload();
    await expect(runPanel(run.id).getByRole('checkbox')).toHaveCount(3);

    // Existing resume validation remains intact; an error preserves the selection.
    const saved=await request('GET','/cases/'+c.id);
    workerState(run.id,{revision:saved.revision-1});
    for(const checkbox of await runPanel(run.id).getByRole('checkbox').all())await expect(checkbox).toBeChecked();
    for(const checkbox of await runPanel(run.id).getByRole('checkbox').all())await checkbox.uncheck();
    await runPanel(run.id).getByRole('checkbox').first().check();
    await runPanel(run.id).getByRole('button',{name:'使用选定的 1 份案例继续'}).click();
    await expect(runPanel(run.id).getByRole('alert')).toContainText('材料已更新');
    await expect(runPanel(run.id).getByRole('checkbox').first()).toBeChecked();
    workerState(run.id,{revision:saved.revision});
    const resumedResponse=page.waitForResponse(r=>r.url().endsWith('/runs/'+run.id+'/resume')&&r.status()===202);
    await runPanel(run.id).getByRole('button',{name:'使用选定的 1 份案例继续'}).click();
    await resumedResponse;
    await expect(runPanel(run.id).getByRole('checkbox')).toHaveCount(0);
    await expect(runPanel(run.id)).toContainText('等待继续');
    expect((await request('GET','/runs/'+run.id)).resume_payload).toEqual({doc_ids:[candidates[0].document_id]});
    await expect(page).toHaveURL(caseUrl+'/research?run='+run.id);
    workerState(run.id,{status:'succeeded',stage:'研究完成'},true);
    await expect(page.getByRole('heading',{name:'类案研究验收成果',exact:true})).toBeVisible();
    await expect(runPanel(run.id)).toContainText('已完成');

    // One composer sends a grounded follow-up and preserves drafts on failures/IME.
    const followup='请进一步比较类案的适用限制，并标注来源。';
    await page.getByLabel('研究消息').fill(followup);
    await page.route('**/api/v1/cases/'+c.id+'/requests',route=>route.fulfill({status:503,json:{detail:'测试中的临时连接错误'}}),{times:1});
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await expect(page.getByRole('alert')).toContainText('测试中的临时连接错误');
    await expect(page.getByLabel('研究消息')).toHaveValue(followup);
    await page.getByLabel('研究消息').dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,bubbles:true});
    await expect(page.getByLabel('研究消息')).toHaveValue(followup);
    const followupResponse=page.waitForResponse(r=>r.url().endsWith('/requests')&&r.request().method()==='POST'&&r.status()===202);
    await page.getByLabel('研究消息').press('Enter');
    const followupRun=await (await followupResponse).json();
    const followupSnapshot=(await request('GET','/runs/'+followupRun.id)).snapshot;
    expect(followupSnapshot.research_context.id).toBe('research-ui-artifact');
    await expect(page).toHaveURL(caseUrl+'/research?run='+followupRun.id);
    await expect(page.getByLabel('研究消息')).toHaveValue('');
    await expect(page.locator('.research-message.user').last()).toContainText(followup);
    await expect(page.getByRole('textbox')).toHaveCount(1);
    workerState(followupRun.id,{status:'failed',stage:'核查引用',error:'回答中的案号无法在本轮来源中核对：(2025)测试999号。未保存本次回答，请重试。'});
    await expect(page.getByRole('alert')).toContainText('(2025)测试999号');
    await expect(page.getByRole('heading',{name:'类案研究验收成果',exact:true})).toBeVisible();
    await runPanel(followupRun.id).getByRole('button',{name:'恢复原任务',exact:true}).click();
    await expect(runPanel(followupRun.id)).toContainText('等待按原输入恢复');
    execFileSync(path.join(root,'.venv/bin/python'),['-c',`
import json,sys
from backend.app.db import connect,dumps,digest,now
args=json.load(sys.stdin)
with connect(True) as c:
    r=c.execute('SELECT * FROM runs WHERE id=?',(args['id'],)).fetchone()
    parent=c.execute('SELECT * FROM artifacts WHERE id=?',('research-ui-artifact',)).fetchone()
    body='# 追问后的研究回复\\n\\n这是通过测试来源检查后的新版本。'
    structured=json.loads(parent['structured'])
    structured['conversation']=[{'question':r['goal'],'answer':'已补充类案适用限制，具体结论请核对所引原文。'}]
    structured['refined_from']=parent['id']
    c.execute('INSERT INTO artifacts (id,case_id,run_id,kind,version,parent_id,title,body,structured,sources,content_hash,checks,completeness,case_revision,created_at,operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
      ('research-ui-followup',r['case_id'],r['id'],'research',2,parent['id'],'追问后的研究回复',body,dumps(structured),'[]',digest(body),dumps({'passed':True}),'limited',r['revision'],now(),'research-ui-followup-complete'))
`],{cwd:root,env,input:JSON.stringify({id:followupRun.id})});
    workerState(followupRun.id,{status:'succeeded',stage:'已完成',error:null});
    await expect(page.getByRole('heading',{name:'追问后的研究回复',exact:true})).toBeVisible();
    await expect(page.locator('.research-chat-turn')).toHaveCount(1);
    await page.reload();
    await expect(page.locator('.research-chat-turn')).toHaveCount(1);
    await expect(page.getByRole('heading',{name:'追问后的研究回复',exact:true})).toBeVisible();
    await page.screenshot({path:path.join(screenshots,'research-chat-completed-desktop.png'),fullPage:true});
    await page.getByRole('button',{name:'新对话',exact:true}).click();
    await expect(page.getByRole('heading',{name:'追问后的研究回复',exact:true})).toHaveCount(0);
    await expect(page.getByRole('region',{name:'常用提示词'})).toBeVisible();
    await page.goto(caseUrl+'/research?run='+run.id);
    await expect(page.getByRole('heading',{name:'追问后的研究回复',exact:true})).toBeVisible();
    await page.getByLabel('选择研究版本').selectOption('research-ui-artifact');
    await expect(page.getByRole('heading',{name:'类案研究验收成果',exact:true})).toBeVisible();
    await expect(page.locator('.research-chat-turn')).toHaveCount(0);

    // Workflow clarification, no-result research and recovery also stay here.
    const second=await request('POST','/cases/'+c.id+'/requests',{goal:'请协助核查这份材料'});
    workerState(second.id,{status:'waiting_input',stage:'等待您的选择',wait_payload:{kind:'workflow',question:'这次希望先完成哪一项？',options:['case.timeline','evidence.gaps','case.assess','document.generate']}});
    await page.goto(caseUrl+'/tasks');
    await expect(logCard(second.id)).toContainText('选择下一步研究内容');
    await logCard(second.id).getByRole('link',{name:'前往结果与研究继续'}).click();
    await runPanel(second.id).getByRole('button',{name:'类案研究',exact:true}).click();
    await expect(runPanel(second.id).getByRole('button',{name:'类案研究',exact:true})).toHaveCount(0);
    expect((await request('GET','/runs/'+second.id)).resume_payload).toEqual({workflow:'case.assess'});
    workerState(second.id,{status:'waiting_input',stage:'等待您的选择',plan:['case.timeline','case.assess'],resume_payload:null,wait_payload:{...wait,candidates:[]}});
    await expect(runPanel(second.id)).toContainText('本轮未检索到候选类案');
    await runPanel(second.id).getByRole('button',{name:'不选案例，继续有限研究'}).click();
    await expect(runPanel(second.id)).toContainText('等待继续');
    expect((await request('GET','/runs/'+second.id)).resume_payload).toEqual({doc_ids:[]});
    workerState(second.id,{status:'failed',stage:'研究暂时失败',error:'测试中的可恢复错误'});
    await expect(runPanel(second.id)).toContainText('测试中的可恢复错误');
    await runPanel(second.id).getByRole('button',{name:'恢复原任务',exact:true}).click();
    await expect(runPanel(second.id)).toContainText('等待按原输入恢复');
    await runPanel(second.id).getByRole('button',{name:'取消任务',exact:true}).click();
    await expect(runPanel(second.id)).toContainText('已取消');
    const restartedResponse=page.waitForResponse(r=>r.url().endsWith('/requests')&&r.request().method()==='POST');
    await runPanel(second.id).getByRole('button',{name:'基于当前材料重试'}).click();
    const restarted=await (await restartedResponse).json();
    await expect(page).toHaveURL(caseUrl+'/research?run='+restarted.id);
    workerState(restarted.id,{status:'waiting_input',stage:'等待您的选择',wait_payload:{...wait,candidates:Array.from({length:6},(_,i)=>({...candidates[0],document_id:'limit-'+i,title:'上限验收类案 '+i}))}});
    await expect(runPanel(restarted.id).getByRole('checkbox')).toHaveCount(6);
    for(let i=0;i<5;i++)await runPanel(restarted.id).getByRole('checkbox').nth(i).check();
    await expect(runPanel(restarted.id).getByRole('checkbox').nth(5)).toBeDisabled();
    await runPanel(restarted.id).getByRole('checkbox').first().uncheck();
    await expect(runPanel(restarted.id).getByRole('checkbox').nth(5)).toBeEnabled();
    await runPanel(restarted.id).getByRole('button',{name:'取消任务',exact:true}).click();
    await expect(runPanel(restarted.id)).toContainText('已取消');

    // Historical paused tasks no longer crowd the main workspace.
    workerState(restarted.id,{status:'waiting_input',stage:'等待您的选择',wait_payload:wait});
    await page.goto(caseUrl+'/research?new=1');
    await expect(page.locator('.research-run')).toHaveCount(0);
    await expect(page.getByRole('region',{name:'开始类案研究'})).toBeVisible();
    await expect(page.getByRole('region',{name:'常用提示词'})).toBeVisible();
    // A history link still opens the older run and its own completed result.
    await page.getByRole('link',{name:'任务记录',exact:true}).click();
    await expect(logCard(run.id)).toContainText('已完成');
    await logCard(run.id).getByRole('link',{name:'打开结果与研究'}).click();
    await expect(runPanel(run.id)).toBeVisible();
    await expect(page.getByRole('heading',{name:'追问后的研究回复',exact:true})).toBeVisible();
    await expect(page.getByText('其他轮次已保存成果',{exact:true})).toHaveCount(0);
    // Collapse preferences persist across reload/routes, while mobile keeps its menu.
    await page.getByRole('button',{name:'收起研究过程与来源',exact:true}).click();
    await page.getByRole('button',{name:'收起左侧导航',exact:true}).click();
    await page.reload();
    await expect(page.getByRole('button',{name:'展开研究过程与来源',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'展开左侧导航',exact:true})).toBeVisible();
    await page.getByRole('complementary',{name:'工作区导航'}).getByRole('link',{name:'我的案件',exact:true}).click();
    await expect(page.getByRole('heading',{name:'我的案件',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'展开左侧导航',exact:true})).toBeVisible();
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button',{name:'菜单',exact:true}).click();
    await expect(page.getByRole('complementary',{name:'工作区导航'}).getByRole('link',{name:'我的案件',exact:true})).toBeVisible();
    await expect(page.locator('.sidebar .navigation-text').filter({hasText:'我的案件'})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
    await page.getByRole('button',{name:'菜单',exact:true}).click();
    expect(errors).toEqual([]);
    const result={passed:true,isolated_workspace:true,worker_states:'fixtures; no model calls',
      research_start_stays_on_page:true,pending_selection_restored:true,selection_survives_polling_and_source:true,
      task_history_has_no_inputs:true,task_history_deep_links:true,resume_validation_and_retry:true,
      workflow_choice:true,empty_candidates:true,selection_limit:true,completed_result_updates:true,
      cancel_restore_and_restart:true,historical_paused_tasks_hidden:true,three_cases_and_three_materials:true,
      reusable_prompts:true,explicit_scope_saved:true,single_chat_composer:true,process_on_right:true,fullscreen_and_restore:true,fullscreen_sources_and_escape:true,collapsed_navigation_and_sources_persist:true,layout_preserves_draft_and_selection:true,compact_topbar:true,mobile_fullscreen_and_navigation:true,followup_scope_and_history:true,failed_send_preserves_draft:true,ime_enter_does_not_send:true,failed_followup_recovery:true,new_chat_has_no_old_report:true,mobile_horizontal_overflow:false,page_errors:errors};
    fs.writeFileSync(path.join(root,'evaluation/browser-research-workspace.json'),JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify(result));
  } finally {
    if(browser)await browser.close();
    if(server.exitCode===null){server.kill('SIGTERM');await once(server,'exit');}
    fs.rmSync(data,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
