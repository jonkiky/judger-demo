// Real browser + API, with a temporary workspace; no existing cases are changed.
const {chromium, expect: baseExpect} = require('../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const expect = baseExpect.configure({timeout: 10000});
const root = path.resolve(__dirname, '..');

async function main() {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'caiyan-case-management-'));
  const socket = net.createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(root, '.venv/bin/python'), ['-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', String(port), '--no-access-log'], {
    cwd: root,
    env: {...process.env, CAIYAN_DATA_DIR: data, LITELLM_LOCAL_MODEL_COST_MAP: 'True'},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '';
  server.stdout.on('data', chunk => { logs += chunk; });
  server.stderr.on('data', chunk => { logs += chunk; });
  let browser;
  try {
    for (let attempt = 0; ; attempt++) {
      if (server.exitCode !== null || attempt > 150) throw new Error(`Test API did not start: ${logs}`);
      try { if ((await fetch(base + '/healthz')).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({channel: 'chrome', headless: true});
    const page = await browser.newPage({viewport: {width: 1440, height: 1080}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // Corpus health is unrelated to case management and needs no model/search service.
    await page.route('**/api/v1/health', route => route.fulfill({json: {corpus: null, services: {}}}));
    const screenshots = path.join(root, 'evaluation/screenshots');
    fs.mkdirSync(screenshots, {recursive: true});
    await page.goto(base + '/cases');
    const credentials = JSON.parse(fs.readFileSync(path.join(data, 'local-credentials.json'), 'utf8'));
    await page.getByLabel('密码', {exact: true}).fill(credentials.password);
    await page.getByRole('button', {name: '进入工作区'}).click();
    await expect(page.getByRole('heading', {name: '我的案件', exact: true})).toBeVisible();
    const me = await (await page.request.get(base + '/api/v1/me')).json();
    const request = async (method, endpoint, body, revision) => {
      const response = await page.request.fetch(base + '/api/v1' + endpoint, {
        method, data: body,
        headers: {'X-CSRF-Token': me.csrf, ...(revision === undefined ? {} : {'If-Match': String(revision)})},
      });
      if (!response.ok()) throw new Error(`${method} ${endpoint}: ${response.status()} ${await response.text()}`);
      return response.json();
    };
    await request('POST', '/cases', {title: '应保留的案件', claims: '核查合同履行情况'});
    await page.getByRole('button', {name: '新建案件', exact: true}).click();
    await page.getByLabel('案件名称', {exact: true}).fill('案件管理验收');
    await page.getByLabel('当前诉求与研究目标').fill('核查逾期交付的合同责任');
    const createdResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/cases') && response.request().method() === 'POST');
    await page.getByRole('button', {name: '建立并添加材料'}).click();
    const created = await (await createdResponse).json();
    await page.waitForURL('**/materials');
    await page.getByRole('link', {name: /我的案件/}).first().click();
    const card = name => page.getByRole('article', {name, exact: true});
    const panel = name => page.getByRole('region', {name, exact: true});
    await card('案件管理验收').getByRole('button', {name: '编辑案件'}).click();
    await panel('编辑案件').getByLabel('案件名称', {exact: true}).fill('未保存的标题');
    await panel('编辑案件').getByRole('button', {name: '取消', exact: true}).click();
    await expect(card('案件管理验收')).toBeVisible();
    await expect(card('案件管理验收').getByRole('button', {name: '编辑案件'})).toBeFocused();
    await card('案件管理验收').getByRole('button', {name: '编辑案件'}).click();
    await panel('编辑案件').getByLabel('案件名称', {exact: true}).fill('已编辑的案件');
    await panel('编辑案件').getByLabel('代理立场').selectOption('被告代理律师');
    await panel('编辑案件').getByLabel('当前诉求与研究目标').fill('核实补充证据和答辩意见');
    await panel('编辑案件').getByLabel('研究时点').fill('2026-11-07');
    await page.screenshot({path: path.join(screenshots, 'case-management-edit.png'), fullPage: true});
    await panel('编辑案件').getByRole('button', {name: '保存修改', exact: true}).click();
    await expect(panel('编辑案件')).toHaveCount(0);
    await expect(card('已编辑的案件')).toBeVisible();
    await page.reload();
    await expect(card('已编辑的案件')).toBeVisible();
    let saved = await request('GET', '/cases/' + created.id);
    expect(saved.position).toBe('被告代理律师');
    expect(saved.claims).toBe('核实补充证据和答辩意见');
    expect(saved.research_date).toBe('2026-11-07');

    // A background update cannot silently overwrite the local edit draft.
    await card('已编辑的案件').getByRole('button', {name: '编辑案件'}).click();
    await panel('编辑案件').getByLabel('案件名称', {exact: true}).fill('保留本地修改');
    const external = {title: '另一页面更新的案件', position: saved.position, claims: saved.claims, research_date: saved.research_date};
    await request('PATCH', '/cases/' + created.id, external, saved.revision);
    await expect(panel('编辑案件').getByRole('alert')).toContainText('案件已更新');
    await expect(panel('编辑案件').getByLabel('案件名称', {exact: true})).toHaveValue('保留本地修改');
    await expect(panel('编辑案件').getByRole('button', {name: '保存修改'})).toBeDisabled();
    await panel('编辑案件').getByRole('button', {name: '重新载入'}).click();
    await expect(panel('编辑案件').getByLabel('案件名称', {exact: true})).toHaveValue(external.title);
    await panel('编辑案件').getByRole('button', {name: '取消', exact: true}).click();
    await page.reload();

    // Cancel does not send a delete, and stale confirmation cannot remove new work.
    let deletes = 0;
    page.on('request', request => { if (request.method() === 'DELETE') deletes++; });
    await card(external.title).getByRole('button', {name: '删除案件'}).click();
    await panel('删除案件').getByRole('button', {name: '取消', exact: true}).click();
    expect(deletes).toBe(0);
    await expect(card(external.title)).toBeVisible();
    await card(external.title).getByRole('button', {name: '删除案件'}).click();
    saved = await request('GET', '/cases/' + created.id);
    await request('PATCH', '/cases/' + created.id, {...external, claims: '删除确认期间补充的目标'}, saved.revision);
    await panel('删除案件').getByRole('button', {name: '确认删除'}).click();
    await expect(panel('删除案件').getByRole('alert')).toContainText('案件已更新');
    await expect(card(external.title)).toBeVisible();
    await panel('删除案件').getByRole('button', {name: '取消', exact: true}).click();
    await page.reload();
    await expect(card(external.title)).toBeVisible();
    await page.screenshot({path: path.join(screenshots, 'case-management-desktop.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.getByLabel('按案件名称搜索').fill(external.title);
    await page.screenshot({path: path.join(screenshots, 'case-management-mobile.png'), fullPage: true});
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    expect(overflow).toBe(false);
    await card(external.title).getByRole('button', {name: '删除案件'}).click();
    await page.screenshot({path: path.join(screenshots, 'case-management-delete.png'), fullPage: true});
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);

    // Failed deletes keep the case and confirmation available for retry.
    const endpoint = base + '/api/v1/cases/' + created.id;
    const failDelete = route => route.request().method() === 'DELETE'
      ? route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: '删除暂时失败，请重试。'})})
      : route.continue();
    await page.route(endpoint, failDelete);
    await panel('删除案件').getByRole('button', {name: '确认删除'}).click();
    await expect(panel('删除案件').getByRole('alert')).toContainText('删除暂时失败');
    await expect(card(external.title)).toBeVisible();
    await page.unroute(endpoint, failDelete);
    await panel('删除案件').getByRole('button', {name: '确认删除'}).click();
    await expect(panel('删除案件')).toHaveCount(0);
    await expect(card(external.title)).toHaveCount(0);
    await expect(page.getByRole('heading', {name: '没有匹配的案件'})).toBeVisible();
    await page.getByLabel('按案件名称搜索').fill('');
    await expect(card('应保留的案件')).toBeVisible();
    await page.reload();
    await expect(card(external.title)).toHaveCount(0);
    await expect(card('应保留的案件')).toBeVisible();
    expect((await page.request.get(endpoint)).status()).toBe(404);
    expect((await request('GET', '/cases')).length).toBe(1);
    await page.setViewportSize({width: 1440, height: 1080});
    await expect(page.getByRole('navigation').first().getByRole('link', {name: /我的案件/})).toContainText('1');
    await page.getByRole('link', {name: '工作台', exact: true}).click();
    await expect(page.getByRole('article', {name: '我的案件', exact: true}).locator('.metric-value strong')).toHaveText('1');
    await expect(page.locator('.dashboard-case')).toHaveCount(1);
    await expect(page.locator('.dashboard-case')).toContainText('应保留的案件');
    await expect(page.locator('.dashboard-case')).not.toContainText(external.title);
    await page.getByRole('link', {name: /我的案件/}).first().click();
    await card('应保留的案件').getByRole('button', {name: '删除案件'}).click();
    await panel('删除案件').getByRole('button', {name: '确认删除'}).click();
    await expect(page.getByRole('heading', {name: '还没有案件'})).toBeVisible();
    expect(errors).toEqual([]);
    const result = {passed: true, isolated_workspace: true, edit_persists: true, edit_cancel: true, edit_conflict: true,
      delete_cancel: true, delete_conflict: true, delete_retry: true, delete_persists: true, last_case_empty_state: true,
      recent_cases_and_count_updated: true, mobile_horizontal_overflow: overflow, page_errors: errors};
    fs.writeFileSync(path.join(root, 'evaluation/browser-case-management.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { server.kill('SIGTERM'); await once(server, 'exit'); }
    fs.rmSync(data, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
