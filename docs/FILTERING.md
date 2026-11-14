# 18 字段组合筛选：实现与验收

更新日期：2026-11-07。适用于全局“类案检索”和案件“结果与研究 → 检索与选案”。

## 数据核对

源 CSV 实际有 **18 列**。原 `index_50k/judgments.sqlite3` 的 documents 表有 15 个技术/业务列，其中保留了 10 个 CSV 字段（含文书内容）。本次按原来的 50,000 个 source_row 恢复其余 8 个字段；没有更换样本、增加案件、重建向量或修改原 SQLite/Qdrant。

新文件 `data/corpus/filters.sqlite3` 为独立筛选索引，约 316.7 MiB。首次构建核对完整 CSV SHA-256、50,000 个行号、全部全文哈希及原已保留的 9 个元数据字段，然后原子发布。共 900,000 个“文书—字段值”关联。后续启动复用同一 release 和 schema 的索引。

| CSV 字段 | API 字段 | 去重选项数（含空值） | 空值文书数 |
|---|---|---:|---:|
| 标题 | `title` | 42,885 | 0 |
| 案号 | `case_number` | 49,855 | 0 |
| 法院 | `court` | 3,014 | 195 |
| 文书类型 | `doc_type` | 9 | 0 |
| 案件类型 | `case_type` | 6 | 321 |
| 审理程序 | `procedure` | 16 | 484 |
| 判决时间 | `judgment_date` | 30 | 0 |
| 发布时间 | `published_at` | 52 | 0 |
| 受理时间 | `accepted_at` | 415 | 15,411 |
| 案例级别 | `case_level` | 1 | 0 |
| 法院级别 | `court_level` | 5 | 432 |
| 案由 | `cause` | 556 | 608 |
| 省 | `province` | 34 | 446 |
| 市 | `city` | 358 | 1,272 |
| 判决年份 | `judgment_year` | 1 | 0 |
| 判决结果 | `outcome` | 226 | 18,738 |
| 法条 | `laws` | 26,525 | 2,318 |
| 文书内容 | `content` | 49,914 | 0 |

新增恢复字段：发布时间、受理时间、案例级别、法院级别、市、判决年份、判决结果、法条。省与市使用源数据原值；不会根据法院名称猜测补齐空值。

## 使用与组合规则

- 18 个字段全部使用可搜索、多选下拉框。任意字段可组合；同一字段多选为 OR，不同字段为 AND。未选择字段表示不限。每字段最多同时选择 50 项，界面显示限额；“全部”无需逐一选择。
- 下拉选项及文书数量按其他已选字段联动计算；不受本字段已选值限制，因此还能增加同一字段的其他值。全库选项数与当前组合下的可选数量分开标示。
- 选项按稳定顺序分页，界面每页 50 项并可继续加载。API 每页上限 100，没有只返回前几种的隐藏截断；每个字段的最后一页均在真实库实测。
- “未载明（空值）”可独立筛选；在选项搜索框输入“未载明”或“空值”也可以找到它。
- 标题、案号、判决结果、法条、文书内容支持搜索完整字段内容以定位选项；选中的是完整字段原值，精确匹配。长内容只在下拉框中显示摘要和字符数，文书内容选项同时显示案号。与全文关键词检索的含义不同：关键词输入在顶部检索框。
- 判决时间可选择一个或多个实际日期；可选的日期区间起止也改为下拉框，包含原有的全部 30 天。倒置区间会被 UI 和 API 拒绝。
- 条件范围数量只计算元数据条件，尚未计入顶部检索词。返回的候选文书数仍是有界检索结果，不是全文命中总数。
- 已选条件可逐项移除、按字段清除或全部清空。无匹配不会自动放宽。检索进行中修改条件会丢弃旧请求的迟到结果。

## 程序与接口

- `backend/app/search_filters.py`：18 字段白名单、值身份、共用参数化 SQL、联动分面与计数。
- `scripts/prepare_filters.py`：只读检查原数据，以临时文件构建并原子替换本 POC 的筛选索引。`run.py` 启动时自动执行。首次构建依赖 manifest 中的原 CSV，可用 `CAIYAN_SOURCE_CSV` 指定同哈希文件的新路径；索引存在后不再依赖 CSV 在线。
- `backend/app/contracts.py`：`filters` 的字段/值格式校验、每字段 50 项上限、分页和日期约束。
- `backend/app/search_service.py`：相同硬条件用于 FTS、向量候选约束和最后一次全文解析核查。Qdrant 仅在允许的片段集合中搜索；语义服务降级仍保持全部条件。
- `frontend/src/SearchFilters.tsx`：通用下拉组件、选项查询缓存、分页、选项搜索、选中标签、范围数量、键盘 Escape 返回触发按钮、手机布局。
- `frontend/src/main.tsx`：两个检索入口复用组件，并阻止迟到搜索响应覆盖新条件。

`POST /api/v1/search/facets` 使用现有登录会话和 CSRF，内部服务另需内部凭据。空对象返回字段目录、50k 范围计数；传入 `field` 返回该字段的可选值。字段值标识为原值的 SHA-256（空值为 `missing`），避免把整篇文书放入请求。请使用接口返回的 `value_id`，不要把显示摘要当作值提交。

```json
{"field":"province","filters":{},"text":"广东","offset":0,"limit":50}
```

选中返回值后，在后续 `/search/facets` 或 `/search` 的 `filters` 字典中传入 `{字段: [value_id, ...]}`。检索接口保留原有 `province`、`court`、`cause`、`date_from`、`date_to` 参数兼容性，与新条件取交集。

选项响应含 `options`、`total_options`、`has_more`、`next_offset`；每个选项含摘要、字符数、空值标记及文书数。`matching_documents` 是全部已选条件的交集数量，`selected` 在零匹配时也保留可识别的已选标签。API 契约已更新至 `docs/openapi.json`。

## 验证记录

- 全部后端测试：34 项通过；新增覆盖全部 18 字段的组合、选项穷举分页、OR/AND、联动计数、空值搜索、全文选项搜索、参数绑定、日期、非法字段和原 CSV 变更拒绝发布。语义故障及越界向量命中使用受控故障测试，验证硬条件不会丢失。
- 真实 50k 服务：18 个字段的数量、首尾页及选项计数与 SQLite 对照通过；真实 Qdrant 混合检索同时应用 18 条件只返回目标文书；矛盾条件返回零；登录、CSRF 与字段白名单通过。见 `evaluation/filter-live-results.json`。
- Chrome：18 下拉框、联动、多选、3 字段组合实际检索、分页/选项搜索、空值、日期下拉、Escape 焦点、390px 手机布局与案件内复用通过；无页面异常、无横向溢出。新增迟到响应测试见 `evaluation/filter-browser-results.json`。
- TypeScript + Vite 生产构建通过。截图在 `evaluation/screenshots/filters-*.png`。
- 验收发现并修复了“空值可以选择，但用其显示名称搜索不到”的问题，加入后端断言和真实浏览器回归。另修正重启时 TIME_WAIT 被误报为端口占用；实际在用监听仍会拒绝启动。

实测本机 41 次分面请求，中位约 96 ms、最大约 2,302 ms；18 条件真实混合检索约 2,810 ms。这是本机验收记录，不是并发容量承诺。

复现命令（后两项需要运行中的服务和本机 Chrome）：

```sh
.venv/bin/python -m pytest -q --junitxml=evaluation/pytest-results.xml
npm --prefix frontend run build
.venv/bin/python scripts/live_filter_tests.py
node scripts/browser_filter_test.cjs
```
