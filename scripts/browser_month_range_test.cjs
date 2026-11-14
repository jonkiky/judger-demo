// Frontend request-contract checks with isolated API fixtures; never edits real cases.
const {chromium, expect: baseExpect} = require('../frontend/node_modules/@playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('../frontend/node_modules/typescript');
const expect = baseExpect.configure({timeout: 10000});
const root = path.resolve(__dirname, '..');
const helpers = {};
new Function('exports', ts.transpileModule(fs.readFileSync(path.join(root, 'frontend/src/monthRange.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS},
}).outputText)(helpers);

for (const [month, end] of [['2024-02','2024-02-29'], ['2025-02','2025-02-28'], ['2000-02','2000-02-29'],
  ['2100-02','2100-02-28'], ['2025-04','2025-04-30'], ['2025-12','2025-12-31'], ['0004-02','0004-02-29']]) {
  assert.equal(helpers.monthBoundary(month), month + '-01');
  assert.equal(helpers.monthBoundary(month, true), end);
}
assert.equal(helpers.monthBoundary(''), '');
assert.equal(helpers.rangeDateLabel('2024-02-29', true), '2024-02');
assert.equal(helpers.rangeDateLabel('2024-02-12', true), '2024-02-12');
assert.deepEqual(helpers.wholeMonthRange({date_from: null, date_to: null}), {date_from: null, date_to: null});

async function main() {
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1080}});
    const errors = [], facets = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    const cid = 'month_range_test';
    const caseData = {id: cid, owner_id: 'tester', title: '月份区间验收', claims: '核查合同履行情况', position: '原告代理律师',
      research_date: '2026-11-07', revision: 1, source_revision: 1, updated_at: '2026-11-07T12:00:00Z',
      materials: [], artifacts: [], runs: [], material_count: 0, waiting_count: 0};
    let saved = {version: 1, options: {query: '', mode: 'semantic', limit: 10, filters: {}, province: '', court: '', cause: '',
      date_from: '2024-02-10', date_to: '2024-02-12'}, response: null, analyzed_options: null,
      analyzed_at: null, analyzed_source_revision: null, source_revision: 1, count: 0,
      updated_at: '2026-11-07T12:00:00Z', suggested_query: '核查合同履行情况', query_warnings: [], readable_material_count: 0};
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), url = new URL(request.url()).pathname.slice('/api/v1'.length);
      const method = request.method();
      let result;
      if (url === '/me') result = {id: 'tester', name: '测试律师', role: 'lawyer', csrf: 'fixture'};
      else if (url === '/cases') result = [caseData];
      else if (url === '/cases/' + cid) result = caseData;
      else if (url === '/search/facets') {
        facets.push(request.postDataJSON());
        result = {fields: [], selected: {}, matching_documents: 10, options: [], total_options: 0, has_more: false, next_offset: null};
      } else if (url === '/cases/' + cid + '/similar-cases' && method === 'GET') result = saved;
      else if (url === '/cases/' + cid + '/similar-cases' && method === 'PUT') {
        const body = request.postDataJSON();
        assert.equal(body.version, saved.version);
        writes.push(body.options);
        saved = {...saved, version: saved.version + 1, options: body.options};
        result = saved;
      } else if (url === '/cases/' + cid + '/similar-cases/analyze' && method === 'POST') {
        const body = request.postDataJSON();
        writes.push(body.options);
        saved = {...saved, version: saved.version + 1, options: body.options, analyzed_options: body.options,
          analyzed_at: '2026-11-07T12:01:00Z', analyzed_source_revision: 1,
          response: {items: [], warnings: [], retrieval_mode: 'semantic', applied_filters: {...body.options, query: '核查合同履行情况'}}};
        result = saved;
      } else throw new Error(`Unexpected API request: ${method} ${url}`);
      await route.fulfill({json: result});
    });
    const from = page.getByLabel('判决起始月份', {exact: true});
    const to = page.getByLabel('判决结束月份', {exact: true});
    const save = page.getByRole('button', {name: '保存条件', exact: true});
    const openFilters = async () => {
      await page.getByRole('button', {name: /筛选条件/}).click();
      await page.locator('.facet-date-range summary').click();
    };
    await page.goto('http://127.0.0.1:8765/cases/' + cid + '/similar-cases');
    await expect(page.getByRole('heading', {name: '类案', exact: true})).toBeVisible();
    await openFilters();
    await expect(from).toHaveAttribute('type', 'month');
    await expect(to).toHaveAttribute('type', 'month');
    await expect(from).toHaveValue('2024-02');
    await expect(to).toHaveValue('2024-02');
    await expect(page.getByText('原有日期范围已按整月展开，请保存条件或重新分析。')).toBeVisible();
    await save.click();
    await expect(save).toBeDisabled();
    assert.equal(writes.at(-1).date_from, '2024-02-01');
    assert.equal(writes.at(-1).date_to, '2024-02-29');
    await page.reload();
    await openFilters();
    await expect(from).toHaveValue('2024-02');
    await expect(to).toHaveValue('2024-02');
    await expect(save).toBeDisabled();
    await from.fill('');
    await save.click();
    await expect(save).toBeDisabled();
    assert.equal(writes.at(-1).date_from, null);
    assert.equal(writes.at(-1).date_to, '2024-02-29');
    await to.fill('');
    await save.click();
    await expect(save).toBeDisabled();
    assert.equal(writes.at(-1).date_to, null);
    for (const [month, end] of [['2025-02','2025-02-28'], ['2025-04','2025-04-30']]) {
      await from.fill(month);
      await to.fill(month);
      await save.click();
      await expect(save).toBeDisabled();
      assert.equal(writes.at(-1).date_from, month + '-01');
      assert.equal(writes.at(-1).date_to, end);
    }
    await from.fill('2025-12');
    await to.fill('2026-01');
    await page.getByRole('button', {name: '分析类案', exact: true}).click();
    await expect(page.getByRole('button', {name: '重新分析', exact: true})).toBeEnabled();
    assert.equal(writes.at(-1).date_from, '2025-12-01');
    assert.equal(writes.at(-1).date_to, '2026-01-31');
    await page.locator('.saved-query summary').click();
    await expect(page.locator('.saved-query')).toContainText('判决时间 2025-12 至 2026-01');
    await from.fill('2026-03');
    await expect(save).toBeDisabled();
    await expect(page.getByRole('button', {name: '重新分析', exact: true})).toBeDisabled();
    await expect(page.locator('.facet-scope')).toContainText('起始月份不得晚于结束月份');
    await to.fill('2026-03');
    await expect(save).toBeEnabled();
    await expect(page.locator('.facet-date-range summary')).toHaveText('判决时间区间（可选） · 2026-03 至 2026-03');
    const screenshots = path.join(root, 'evaluation/screenshots');
    fs.mkdirSync(screenshots, {recursive: true});
    await page.locator('.facet-date-range').screenshot({path: path.join(screenshots, 'month-range-desktop.png')});
    await page.setViewportSize({width: 390, height: 844});
    await from.scrollIntoViewIfNeeded();
    await expect(from).toBeVisible();
    await expect(to).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.locator('.facet-date-range').screenshot({path: path.join(screenshots, 'month-range-mobile.png')});
    assert(facets.some(request => request.date_from === '2024-02-01' && request.date_to === '2024-02-29'));
    assert(!facets.some(request => request.field === 'judgment_date'));
    expect(errors).toEqual([]);
    const report = {passed: true, api_fixtures: true, existing_cases_unchanged: true, month_only_inputs: true,
      leap_year_and_month_ends: true, legacy_range_normalized: true, save_and_analyze_payloads: true,
      open_ended_ranges: true, reversed_range_blocked: true, no_daily_options_fetch: true, mobile_overflow: false};
    fs.writeFileSync(path.join(root, 'evaluation/browser-month-range.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}

main().catch(error => {console.error(error); process.exitCode = 1;});
