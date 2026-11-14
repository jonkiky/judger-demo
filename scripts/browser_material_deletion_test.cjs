// Exercise the built UI and real API in temporary storage; existing cases are untouched.
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
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'caiyan-material-deletion-'));
  const socket = net.createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(root, '.venv/bin/python'), ['-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', String(port), '--no-access-log'], {
    cwd: root, env: {...process.env, CAIYAN_DATA_DIR: data, LITELLM_LOCAL_MODEL_COST_MAP: 'True'},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let logs = '', browser, releaseDelete;
  server.stdout.on('data', chunk => { logs += chunk; });
  server.stderr.on('data', chunk => { logs += chunk; });
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
    await page.goto(base + '/cases');
    const credentials = JSON.parse(fs.readFileSync(path.join(data, 'local-credentials.json'), 'utf8'));
    await page.getByLabel('密码', {exact: true}).fill(credentials.password);
    await page.getByRole('button', {name: '进入工作区'}).click();
    await expect(page.getByRole('heading', {name: '我的案件', exact: true})).toBeVisible();
    const me = await (await page.request.get(base + '/api/v1/me')).json();
    async function request(method, endpoint, body) {
      const response = await page.request.fetch(base + '/api/v1' + endpoint, {method, data: body, headers: {'X-CSRF-Token': me.csrf}});
      if (!response.ok()) throw new Error(`${method} ${endpoint}: ${response.status()} ${await response.text()}`);
      return response.json();
    }
    const c = await request('POST', '/cases', {title: '材料删除验收', claims: '核查材料与引用'});
    const other = await request('POST', '/cases', {title: '其他案件'});
    const otherMaterial = await request('POST', '/cases/' + other.id + '/materials/text', {name: '其他案件材料', text: '必须保留的内容。'});
    const original = await request('POST', '/cases/' + c.id + '/materials/text', {name: '合同摘录', text: '约定于2026年5月31日交付。'});
    const targetName = '合同摘录（修订）';
    const target = await request('POST', '/materials/' + original.id + '/versions', {name: targetName, text: '修订后的交付约定，仅用于删除测试。'});
    await page.goto(base + '/cases/' + c.id + '/materials');
    const uploadedResponse = page.waitForResponse(r => r.url().endsWith('/materials') && r.request().method() === 'POST');
    await page.getByLabel('上传案件材料').setInputFiles({name: '付款凭证.txt', mimeType: 'text/plain', buffer: Buffer.from('买方支付人民币十万元。')});
    const uploaded = await (await uploadedResponse).json();
    const row = name => page.getByRole('row').filter({has: page.getByRole('button', {name, exact: true})});
    const panel = page.getByRole('region', {name: '删除案件材料', exact: true});
    const deleteButton = name => page.getByRole('button', {name: '删除材料 ' + name, exact: true});
    await expect(row('付款凭证.txt')).toBeVisible();
    await page.getByRole('button', {name: targetName, exact: true}).click();
    const source = page.getByRole('region', {name: '来源原文'});
    await expect(source.getByLabel('来源全文')).toContainText('修订后的交付约定');
    let deleteRequests = 0;
    page.on('request', r => { if (r.method() === 'DELETE' && r.url().includes('/materials/')) deleteRequests++; });
    await deleteButton(targetName).click();
    await expect(panel).toContainText(targetName);
    await panel.getByRole('button', {name: '取消', exact: true}).click();
    expect(deleteRequests).toBe(0);
    await expect(deleteButton(targetName)).toBeFocused();
    await expect(row(targetName)).toBeVisible();

    await page.getByLabel('搜索材料名称').fill('合同');
    await deleteButton(targetName).click();
    const endpoint = base + '/api/v1/materials/' + target.id;
    const failDelete = route => route.request().method() === 'DELETE'
      ? route.fulfill({status: 500, json: {detail: '删除暂时失败，请重试。'}}) : route.continue();
    await page.route(endpoint, failDelete);
    await panel.getByRole('button', {name: '确认删除材料', exact: true}).click();
    await expect(panel.getByRole('alert')).toContainText('删除暂时失败');
    await expect(row(targetName)).toBeVisible();
    await page.unroute(endpoint, failDelete);
    const gate = new Promise(resolve => { releaseDelete = resolve; });
    const delayDelete = async route => { if (route.request().method() === 'DELETE') await gate; await route.continue(); };
    await page.route(endpoint, delayDelete);
    await panel.getByRole('button', {name: '确认删除材料', exact: true}).click();
    await expect(panel.getByRole('button', {name: '正在删除…'})).toBeDisabled();
    await expect(panel.getByRole('button', {name: '取消', exact: true})).toBeDisabled();
    await expect(panel.getByRole('button', {name: '收起表单'})).toBeDisabled();
    await expect(deleteButton(targetName)).toBeDisabled();
    releaseDelete();
    await expect(panel).toHaveCount(0);
    await page.unroute(endpoint, delayDelete);
    expect(deleteRequests).toBe(2);
    await expect(row(targetName)).toHaveCount(0);
    await expect(page.getByRole('heading', {name: '没有匹配的材料'})).toBeVisible();
    await expect(source.getByRole('alert')).toContainText('材料不存在或已撤回');
    await expect(source.getByLabel('来源全文')).toHaveCount(0);
    await page.getByRole('button', {name: '关闭来源'}).click();
    await page.getByLabel('搜索材料名称').fill('');
    await expect(row('付款凭证.txt')).toBeVisible();
    for (const id of [original.id, target.id]) {
      expect((await page.request.get(base + '/api/v1/materials/' + id)).status()).toBe(404);
      expect((await page.request.get(base + '/api/v1/materials/' + id + '/download')).status()).toBe(404);
    }
    await page.reload();
    await expect(row('付款凭证.txt')).toBeVisible();
    await expect(row(targetName)).toHaveCount(0);
    expect((await request('GET', '/cases/' + c.id)).materials.map(m => m.id)).toEqual([uploaded.id]);
    expect((await request('GET', '/cases')).find(item => item.id === c.id).material_count).toBe(1);
    expect((await request('GET', '/materials/' + otherMaterial.id)).text).toBe('必须保留的内容。');

    await page.setViewportSize({width: 390, height: 844});
    await deleteButton('付款凭证.txt').click();
    const screenshots = path.join(root, 'evaluation/screenshots');
    fs.mkdirSync(screenshots, {recursive: true});
    await page.screenshot({path: path.join(screenshots, 'material-delete-mobile.png'), fullPage: true});
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await panel.getByRole('button', {name: '确认删除材料', exact: true}).click();
    await expect(page.getByRole('heading', {name: '尚未添加材料'})).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', {name: '尚未添加材料'})).toBeVisible();
    expect((await request('GET', '/cases')).find(item => item.id === c.id).material_count).toBe(0);
    expect(errors).toEqual([]);
    const result = {passed: true, isolated_workspace: true, cancel: true, retry: true, busy_protection: true,
      versions_revoked: true, cached_source_cleared: true, other_materials_preserved: true,
      deletion_persists: true, filtered_empty_state: true, final_empty_state: true, mobile_overflow: false, page_errors: errors};
    fs.writeFileSync(path.join(root, 'evaluation/browser-material-deletion.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    releaseDelete?.();
    if (browser) await browser.close();
    if (server.exitCode === null) { server.kill('SIGTERM'); await once(server, 'exit'); }
    fs.rmSync(data, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
