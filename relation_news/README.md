# 公司实体知识图谱 · 关系舆情采集与结构化

为构建「公司—关系—公司」知识图谱采集舆情数据。把公司间关系按"从哪个平台、用什么关键词搜得到"
归成 **两大类**，对应两个采集脚本；再用大模型把公告结构化成统一 JSON，供后续连图。

当前覆盖 **10 家公司**：立讯精密、佰维存储、中芯国际、中信证券、工业富联、郑州煤电、兆易创新、京能电力、蔚蓝锂芯、恒瑞医药。
时间跨度：最近约 365 天（可在脚本顶部 `DATE_START` 调整）。

---

## 一、目录结构

```
relation_news/                      ← 执行目录（放脚本与本 README）
├── cninfo_crawler.py               大类一：巨潮公告采集 + 全文 + 对手方抽取
├── extract_util.py                 对手方实体抽取工具（被 cninfo_crawler 调用）
├── to_yuqing_json.py               公告 -> 舆情结构化 JSON（调 qwen-vl-plus）
├── news_crawler.py                 大类二：财经新闻舆情采集框架（需抓包后启用）
├── README.md
├── output/                         ← 最终结果：舆情_公司_代码.json（每家一个）
└── intermediate/                   ← 全部中间产物，勿与脚本混放
    ├── csv/    result_cninfo.csv（公告索引）、result_news.csv（新闻索引）
    ├── jsonl/  result_cninfo.jsonl（含公告全文，喂大模型的输入）
    ├── text/   逐条公告全文 .txt
    ├── pdf/    （可选）公告 PDF 原件，cninfo_crawler 里 SAVE_PDF=True 时生成
    └── cache/  cache_公司.jsonl（大模型结果缓存，重跑不重复调用）
```

> 所有子目录由脚本运行时自动创建，无需手动建。
> 立讯精密的历史结果请自行剪切到 `output/` 与 `intermediate/` 对应位置。

---

## 二、关系类型 → 采集大类

| 大类 | 覆盖的关系类型 | 平台 | 脚本 |
|---|---|---|---|
| **一｜法定披露关系** | 股权与控制（母子公司/控股参股/实控人/一致行动人）、并购重组、对外投资增资、担保、关联交易、诉讼仲裁、监管处罚 | 巨潮资讯网（强制披露，标题带关键词） | `cninfo_crawler.py` |
| **二｜市场舆情关系** | 业务合作/合资、供应链（供应商-客户）、竞争、未披露的投资并购动态、行业地位 | 财经新闻搜索（无强制披露，散落新闻） | `news_crawler.py` |

---

## 三、运行流程

### 准备
```bash
pip install requests pdfplumber
pip install pymupdf            # 可选，pdfplumber 抽不出时的兜底

# 大模型 Key（DashScope）
set DASHSCOPE_API_KEY=sk-xxxx          # Windows
export DASHSCOPE_API_KEY=sk-xxxx       # Linux / macOS
```

### 第 1 步：采集公告 + 全文 + 对手方
```bash
python cninfo_crawler.py
```
产出：`intermediate/jsonl/result_cninfo.jsonl`（含全文，下一步输入）、
`intermediate/csv/result_cninfo.csv`（索引，可用 Excel 浏览）、`intermediate/text/*.txt`。

### 第 2 步：结构化成舆情 JSON
```bash
python to_yuqing_json.py
```
读 `result_cninfo.jsonl`，逐条调用 `qwen-vl-plus` 抽取语义字段，按公司输出到
`output/舆情_{公司}_{代码}.json`。已抽过的公告走缓存、不重复调用模型。

### （可选）第 2′ 步：采集新闻舆情（大类二）
`news_crawler.py` 的新闻接口需先抓包确认（见第六节），确认后产出
`intermediate/csv/result_news.csv`。

---

## 四、最终 JSON 字段说明

每个 `output/舆情_*.json` 是一个事件数组，单条字段：

| 字段 | 来源 | 说明 |
|---|---|---|
| event_id | 程序 | `代码_YYYYMMDD_序号` |
| company_name / ts_code | 程序 | 公司全称 / 带交易所后缀代码（.SH/.SZ/.BJ） |
| source / pub_time / title / content / url | 程序 | 来源、发布时间、标题、正文、原文链接 |
| event_type / event_subtype | **模型** | 一级/二级事件类型 |
| event_time | 模型→程序兜底 | 事件实际发生日；模型取不到则回填为公告日期 |
| company_role | 模型 | 本公司在事件中的角色 |
| related_companies | 模型 | `[{company_name, role}]`，连图用的对端公司 |
| summary / keywords / sentiment / importance | 模型 | 一句话摘要 / 关键词 / 情感 / 重要性 1-5 |
| _announcementId | 程序 | 溯源/去重用，非 schema 字段 |

设计原则：**确定性字段（标题/链接/时间/代码等）由程序填，只有需要"理解"的语义字段交给模型**，省 token 且不会被模型改坏已知数据。

---

## 五、关键可调项（脚本顶部配置区）

- `cninfo_crawler.py`
  - `COMPANIES`：公司清单（简称即可，代码自动解析）。
  - `DATE_START/DATE_END`：时间跨度。
  - `KEYWORD_GROUPS`：各关系大类的标题关键词。
  - `TITLE_BLACKLIST`：丢弃纯内部治理文件（管理制度/办法/草案等）。
  - `SAVE_PDF`：是否保留 PDF 原件。
- `to_yuqing_json.py`
  - `MODEL`：默认 `qwen-vl-plus`；纯文本任务用 `qwen-plus`/`qwen-max` 通常更稳更省。
  - `COMPANY_FULLNAME`：公司全称表，扩展公司时补充（缺失回退用简称）。
  - `RELATED_BLACKLIST`：从 related_companies 剔除登记结算/交易所等基础设施机构（保守，不滤监管机构）。
  - `SYSTEM_PROMPT`：抽取提示词与受控词表，调整事件分类口径改这里。

---

## 六、新闻接口抓包 SOP（启用 news_crawler 用）

新闻站接口变动频繁，**不写死 URL**。在 Chrome：
1. 打开目标站搜索页，F12 → Network，过滤 Fetch/XHR，清空。
2. 搜索"立讯精密"回车 / 点下一页。
3. 找返回 JSON 且含新闻标题的请求，Headers 记 URL/参数，Preview 看字段名。
4. 右键 → Copy as cURL → 贴 curlconverter.com 转 Python，填进 `news_crawler.py`
   的适配器，把 `VERIFIED` 改 `True` 并在 `ADAPTERS` 注册。

---

## 七、合规与稳定性

- 已内置：随机 UA、请求间隔、自动重试退避、按 ID 去重、扫描件检测、模型结果缓存。
- 巨潮为法定披露平台、公开数据；仍请控制频率（默认每请求 1–2 秒），勿高并发。
- 新闻站注意 robots.txt 与版权，正文优先存链接+摘要。
- 人员兼任、失信被执行等关系建议走企查查/天眼查官方 API 或中国执行信息公开网，不建议爬取。

---

## 八、下一步（待开发）

把 `output/*.json` 连成图：以 `company_name` 为主体节点、`related_companies` 为对端节点、
`event_type` + role 映射为边类型（控股/收购/担保/关联交易/诉讼等），输出边表（CSV / Cypher / NetworkX）。
