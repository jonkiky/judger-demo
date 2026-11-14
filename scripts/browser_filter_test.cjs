const {chromium,expect:baseExpect}=require('../frontend/node_modules/@playwright/test');
const expect=baseExpect.configure({timeout:20000});
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
let activePage,activeBrowser;
const facetRequests=[];

(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  activePage=page;activeBrowser=browser;
  page.on('response',async response=>{
    if(!response.url().endsWith('/api/v1/search/facets'))return;
    try{const req=response.request().postDataJSON();const data=await response.json();facetRequests.push({field:req.field,text:req.text,status:response.status(),options:data.total_options,matching:data.matching_documents})}catch{}
  });
  const errors=[];const checks=[];
  page.on('pageerror',e=>errors.push(e.message));
  const credentials=JSON.parse(fs.readFileSync(path.join(root,'data/local-credentials.json')));
  await page.goto('http://127.0.0.1:8765');
  await page.getByLabel('密码',{exact:true}).fill(credentials.password);
  await page.getByRole('button',{name:'进入工作区'}).click();
  await page.getByRole('heading',{name:'案情有序，研究有据。'}).waitFor();
  await page.goto('http://127.0.0.1:8765/search');
  await page.getByRole('button',{name:'筛选条件',exact:true}).click();
  await expect(page.locator('.facet-trigger')).toHaveCount(18);
  await expect(page.locator('.facet-scope')).toContainText('50,000 份文书');
  checks.push('all_18_dropdowns_visible');
  await page.screenshot({path:path.join(root,'evaluation/screenshots/filters-desktop-all.png'),fullPage:true});

  async function open(field){
    await page.getByRole('button',{name:field+'筛选',exact:true}).click();
    return page.getByRole('dialog',{name:field+'筛选选项',exact:true});
  }
  async function select(field,label){
    const popup=await open(field);
    await popup.getByRole('searchbox').fill(label);
    await popup.getByRole('checkbox',{name:new RegExp('^'+label)}).check({timeout:20000});
    await popup.getByRole('button',{name:'完成',exact:true}).click();
    await expect(page.getByRole('button',{name:field+'筛选',exact:true})).toContainText(label);
  }
  await select('省','广东省');
  let popup=await open('市');
  await expect(popup.getByRole('checkbox',{name:/^惠州市/})).toBeVisible();
  await expect(popup.getByRole('checkbox',{name:/^杭州市/})).toHaveCount(0);
  await popup.getByRole('checkbox',{name:/^惠州市/}).check();
  await popup.getByRole('button',{name:'完成',exact:true}).click();
  await select('案件类型','民事');
  checks.push('province_city_linked_dropdown_and_imported_city_selection');
  await page.getByLabel('类案检索问题').fill('(2025)粤1303民初9812号');
  await page.getByLabel('检索方式').selectOption('case_number');
  const response=page.waitForResponse(r=>r.url().endsWith('/api/v1/search')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'检索类案',exact:true}).click();
  const result=await (await response).json();
  if(result.items?.length!==1||Object.keys(result.applied_filters.filters).length!==3)throw new Error('Combined browser filters not enforced');
  await expect(page.locator('.search-result')).toHaveCount(1);
  await page.screenshot({path:path.join(root,'evaluation/screenshots/filters-desktop-results.png'),fullPage:true});
  checks.push('three_field_combination_submitted_and_real_judgment_returned');

  // Hold a real HTTP response until after the user changes the filters.
  let release, ready;
  const held=new Promise(resolve=>release=resolve);
  const fetched=new Promise(resolve=>ready=resolve);
  const handler=async route=>{const response=await route.fetch();ready();await held;await route.fulfill({response})};
  await page.route('**/api/v1/search',handler);
  const pending=page.waitForResponse(r=>r.url().endsWith('/api/v1/search')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'检索类案',exact:true}).click();
  await fetched;
  await page.getByRole('button',{name:'清空筛选',exact:true}).click();
  release();
  await (await pending).finished();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await expect(page.locator('.search-result')).toHaveCount(0);
  await page.unroute('**/api/v1/search',handler);
  checks.push('late_response_cannot_restore_results_for_cleared_filters');
  await expect(page.locator('.facet-scope')).toContainText('50,000 份文书');
  popup=await open('省');
  await popup.getByRole('checkbox',{name:/^广东省/}).check();
  await popup.getByRole('checkbox',{name:/^浙江省/}).check();
  await popup.getByRole('button',{name:'完成',exact:true}).click();
  await expect(page.getByRole('button',{name:'省筛选',exact:true})).toContainText('已选 2 项');
  popup=await open('市');
  await expect(popup.getByRole('checkbox',{name:/^杭州市/})).toBeVisible();
  await expect(popup.getByRole('checkbox',{name:/^惠州市/})).toBeVisible();
  await popup.getByRole('searchbox').press('Escape');
  await expect(page.getByRole('button',{name:'市筛选',exact:true})).toBeFocused();
  checks.push('or_multiselect_preserved_and_escape_restores_focus');
  await page.getByRole('button',{name:'清空筛选',exact:true}).click();
  await select('法院级别','未载明');
  await expect(page.locator('.facet-scope')).toContainText('432 份文书');
  checks.push('missing_imported_metadata_is_selectable');
  await page.getByRole('button',{name:'清空筛选',exact:true}).click();
  popup=await open('法院');
  await expect(popup.getByRole('checkbox')).toHaveCount(50);
  await popup.getByRole('button',{name:'加载更多选项',exact:true}).click();
  await expect(popup.getByRole('checkbox')).toHaveCount(100);
  await popup.getByRole('searchbox').fill('惠阳');
  await expect(popup.getByRole('checkbox',{name:/惠阳/})).toBeVisible();
  await expect(popup.getByRole('checkbox')).toHaveCount(1);
  await popup.getByRole('searchbox').press('Escape');
  checks.push('high_cardinality_options_paginate_and_search');

  await page.locator('.facet-date-range summary').click();
  await page.getByLabel('判决时间从',{exact:true}).selectOption('2025-06-01');
  await page.getByLabel('判决时间至',{exact:true}).selectOption('2025-06-02');
  await expect(page.locator('.facet-scope')).not.toContainText('50,000 份文书');
  await page.getByRole('button',{name:'清空筛选',exact:true}).click();
  checks.push('date_range_uses_dropdowns');

  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=0);
  popup=await open('市');
  await expect(popup.getByRole('checkbox').first()).toBeVisible();
  const box=await popup.boundingBox();
  if(box.x<0||box.x+box.width>390)throw new Error('Dropdown leaves mobile viewport');
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
  if(overflow)throw new Error('Mobile horizontal overflow');
  await page.screenshot({path:path.join(root,'evaluation/screenshots/filters-mobile-dropdown.png'),fullPage:true});
  await popup.getByRole('searchbox').press('Escape');
  checks.push('mobile_dropdown_within_viewport_no_horizontal_overflow');
  // Both the global search and case research use the same live component.
  const existing=JSON.parse(fs.readFileSync(path.join(root,'evaluation/acceptance-runs.json')));
  await page.goto('http://127.0.0.1:8765/cases/'+existing.case_id+'/research');
  await page.getByRole('button',{name:'检索与选案',exact:true}).click();
  await page.getByRole('button',{name:'筛选条件',exact:true}).click();
  await expect(page.locator('.facet-trigger')).toHaveCount(18);
  checks.push('case_research_has_same_18_dropdowns');
  if(errors.length)throw new Error(errors.join('\n'));
  const report={checks,page_errors:errors,mobile_overflow:overflow,dropdown_bounds:box,real_document:result.items[0].document_id};
  fs.writeFileSync(path.join(root,'evaluation/filter-browser-results.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report,null,2));
  await browser.close();
})().catch(async e=>{
  console.error(e);
  fs.writeFileSync(path.join(root,'evaluation/filter-browser-failure.json'),JSON.stringify({error:String(e),facetRequests},null,2));
  if(activePage)await activePage.screenshot({path:path.join(root,'evaluation/screenshots/filters-failure.png'),fullPage:true}).catch(()=>{});
  if(activeBrowser)await activeBrowser.close();
  process.exitCode=1;
});
