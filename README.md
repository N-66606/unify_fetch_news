# 公司实体知识图谱 · 舆情数据采集与结构化

为构建「公司—关系—公司」知识图谱而设计的多源舆情/公告采集与结构化工具。

从多个财经数据源爬取与目标公司相关的新闻和公告，调用大语言模型进行结构化抽取，输出统一格式的 JSON 事件数据，用于后续图谱连边。

---

## 目录结构

```
sort_news_data/
├── main.py                        # 统一入口：指定数据源一键完成爬取 + 转换
├── config.py                      # 共享配置：公司表、股票代码、API 配置
│
├── relation_news/                 # 大类一：法定披露关系 & 财经新闻
│   ├── cninfo_crawler.py          # 巨潮资讯网公告爬虫
│   ├── cnstock_crawler.py         # 中国证券网新闻爬虫
│   ├── jrj_crawler.py             # 金融界新闻爬虫
│   ├── news_to_json.py            # 统一转换：所有来源 CSV → 舆情 JSON
│   ├── extract_util.py            # 公告对手方实体抽取工具（cninfo 使用）
│   └── diagnose_cninfo.py         # 巨潮接口连通性诊断脚本
│
├── sentiment_scraper/             # 大类二：通用舆情（财联社 & 东方财富）
│   ├── cls_crawler.py             # 财联社独立爬虫（搜索 + 电报流合并）
│   ├── eastmoney_only.py          # 东方财富独立爬虫
│   ├── Scraper.py                 # 原始双源爬虫（财联社部分保留，供参考/定时任务）
│   ├── Visualizer.py              # 数据可视化工具
│   └── exports/                   # 爬虫导出的 CSV（运行时自动创建）
│       ├── 东方财富/              # {公司名}_东方财富.csv
│       └── 财联社/                # {公司名}_财联社.csv
│
├── intermediate/                  # 中间产物（运行时自动创建）
│   ├── csv/                       # relation_news 各爬虫输出的原始 CSV
│   ├── cache/                     # LLM 调用结果缓存（重跑不重复计费）
│   └── text/                      # 公告 PDF 提取的全文 txt（cninfo）
│
└── output/                        # 最终 JSON（运行时自动创建）
    ├── cninfo_json/               # 舆情_cninfo_{公司名}.json
    ├── cnstock_json/              # 舆情_cnstock_{公司名}.json
    ├── jrj_json/                  # 舆情_jrj_{公司名}.json
    ├── eastmoney_json/            # 舆情_eastmoney_{公司名}.json
    └── cls_json/                  # 舆情_cls_{公司名}.json
```

---

## 数据来源与覆盖

| 数据源 | `--source` | 内容类型 | 正文获取 |
|---|---|---|---|
| 巨潮资讯网 | `cninfo` | 上市公司公告（强制披露） | PDF 全文提取 |
| 中国证券网 | `cnstock` | 财经新闻 | HTML 正文解析 |
| 金融界 | `jrj` | 财经新闻 | meta description |
| 东方财富 | `eastmoney` | 财经新闻 | 原文全文 |
| 财联社 | `cls` | 财经电报 + 新闻 | 电报正文 |

**覆盖公司（10 家）：** 立讯精密、佰维存储、中芯国际、中信证券、工业富联、郑州煤电、兆易创新、京能电力、蔚蓝锂芯、恒瑞医药

---

## 快速开始

### 环境准备

```bash
pip install requests beautifulsoup4 lxml pdfplumber urllib3
# cninfo PDF 解析备用库（pdfplumber 失败时自动切换）
pip install pymupdf
```

设置大模型 API Key（用于结构化抽取，通义千问）：

```bash
# Windows
set DASHSCOPE_API_KEY=sk-xxxx
# Linux / Mac
export DASHSCOPE_API_KEY=sk-xxxx
```

> Key 也可以直接写入 `config.py` 的 `API_KEY` 变量（不推荐提交到代码仓库）。

### 基本用法

所有数据源均通过根目录的 `main.py` 统一调用，**财联社已与其他来源完全一致，无需手动操作**。

```bash
# 从项目根目录运行
cd sort_news_data

# 爬取中国证券网 + 转换 JSON（全部公司，最近 180 天）
python main.py --source cnstock

# 指定公司 + 时间段
python main.py --source cnstock --company 立讯精密 中信证券 --date-start 2026-01-01

# 财联社（与其他来源用法完全一致）
python main.py --source cls --company 立讯精密 --date-start 2026-03-01

# 东方财富
python main.py --source eastmoney --company 中芯国际 --date-start 2026-01-01 --date-end 2026-06-01

# 只转换已有 CSV（跳过爬取，适合重新调整 prompt 后重跑）
python main.py --source jrj --skip-crawl

# 只爬取不转换（先攒数据，稍后批量转换）
python main.py --source cninfo --skip-convert --date-start 2026-03-01
```

### 财联社登录配置

财联社接口需要登录态，首次使用前在 `sentiment_scraper/cls_crawler.py` 顶部填入凭据：

```python
CLS_TOKEN  = "你的token"
CLS_UID    = "你的uid"
CLS_COOKIE = "从浏览器 F12 → Network 复制的完整 Cookie"
```

获取方式：浏览器登录 [cls.cn](https://www.cls.cn)，打开 F12 → Network，随意点击页面触发一个请求，从请求头中复制 `Cookie` 字段，并从 Cookie 或用户信息接口中取出 `token` 和 `uid`。**Cookie 有效期约 2 个月**，过期后重新复制即可。

---

## 数据流

```
        爬虫（各 crawler）
              │
              ▼
    sentiment_scraper/exports/  或  intermediate/csv/
           （原始 CSV）
              │
              ▼
    relation_news/news_to_json.py
        （LLM 结构化抽取，带缓存）
              │
              ▼
          output/{source}_json/
        舆情_{source}_{公司名}.json
```

---

## 输出格式

每家公司输出一个 JSON 文件（数组），每条事件结构如下：

```json
{
  "event_id":    "002475_20260530_001",
  "company_name":"立讯精密工业股份有限公司",
  "ts_code":     "002475.SZ",
  "source":      "巨潮资讯网",
  "pub_time":    "2026-05-30 00:00:00",
  "title":       "立讯精密：关于收购XXX公司股权的公告",
  "content":     "正文全文...",
  "url":         "http://static.cninfo.com.cn/...",
  "event_type":  "资本运作",
  "event_subtype":"股权收购",
  "event_time":  "2026-05-28",
  "company_role":"收购方",
  "related_companies": [
    {"company_name": "XXX科技有限公司", "role": "被收购方"}
  ],
  "summary":     "立讯精密拟收购XXX公司51%股权，交易对价约5亿元。",
  "keywords":    ["收购", "股权", "产业链"],
  "sentiment":   "positive",
  "importance":  4
}
```

### event_type 枚举（14 类）

| 类型 | 子类型 |
|---|---|
| 资本运作 | 股权收购 / 资产收购 / 增资入股 / 重大资产重组 / 吸收合并 / 分拆 / 要约收购 / 对外投资 / 设立子公司 |
| 投资并购 | 股权收购 / 资产收购 / 战略投资 / 增持 / 举牌 / 意向收购 |
| 关联交易 | 日常关联交易 / 关联采购 / 关联销售 / 关联资产转让 / 关联租赁 |
| 对外担保 | 为子公司担保 / 为关联方担保 / 担保额度预计 / 互保 |
| 诉讼仲裁 | 民事诉讼 / 商事仲裁 / 行政诉讼 / 执行案件 |
| 监管处罚 | 行政处罚 / 反垄断处罚 / 立案调查 / 警示函 |
| 股权变动 | 股权质押 / 解除质押 / 大股东增持 / 大股东减持 / 股份回购 / 权益变动 |
| 公司治理 | 董事会决议 / 高管变动 / 利润分配 / 审计报告 |
| 业务合作 | 战略合作 / 合资设立 / 联合研发 / 技术授权 / 签约合作 |
| 供应链 | 获得订单 / 供应商关系 / 客户关系 / 供货合同 |
| 竞争动态 | 竞争格局 / 市场份额 / 竞争对手分析 |
| 经营动态 | 业绩说明 / 股东会表态 / 经营策略 / 产能扩张 |
| 研报观点 | 个股研报 / 行业研报 / 评级调整 |
| 行业资讯 | 行业政策 / 市场动态 / 综合资讯 |


---

## 参数说明

### main.py

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--source` | 数据源，必填，可选：`cninfo` `cnstock` `jrj` `eastmoney` `cls` | — |
| `--company` | 公司简称，可多个，空格分隔 | 全部 10 家 |
| `--date-start` | 起始日期 `YYYY-MM-DD` | 最近 180 天 |
| `--date-end` | 截止日期 `YYYY-MM-DD` | 今天 |
| `--skip-crawl` | 跳过爬取，只做转换 | False |
| `--skip-convert` | 只爬取，不做转换 | False |

### 各爬虫（单独运行）

`cninfo / cnstock / jrj / eastmoney / cls` 均支持相同的 CLI 参数：`--company`、`--date-start`、`--date-end`。

```bash
# 单独运行财联社爬虫（只产出 CSV，不做 LLM 转换）
python sentiment_scraper/cls_crawler.py --company 立讯精密 --date-start 2026-03-01

# 单独运行转换
python relation_news/news_to_json.py --source cls --company 立讯精密 --date-start 2026-03-01
```

---

## 扩展：新增公司

**Step 1**：在 `config.py` 中添加公司信息：

```python
COMPANY_FULLNAME["比亚迪"] = "比亚迪股份有限公司"
COMPANY_CODE["比亚迪"]     = "002594.SZ"
```

**Step 2**：`eastmoney_only.py` 和 `cls_crawler.py` 直接使用 `config.py` 中的公司列表，无需额外修改。如果仍在使用 `Scraper.py` 的定时任务模式，则同时在其 `COMPANIES` 列表中添加：

```python
{"name": "比亚迪", "stock_code": "sz002594", "keywords": ["比亚迪", "002594"]},
```

---

## 缓存机制

`news_to_json.py` 对每条记录按 URL MD5（新闻）或 `announcementId`（公告）做缓存，存储在 `intermediate/cache/` 目录。重新运行时已处理的条目直接读缓存，不重复调用 API。

如需强制重跑（如修改了 prompt），删除对应的缓存文件即可：

```bash
# 删除财联社的立讯精密缓存
rm intermediate/cache/cls_cache_立讯精密.jsonl

# 删除 cnstock 的中信证券缓存
rm intermediate/cache/cnstock_cache_中信证券.jsonl
```

---

## 常见问题

**Q：财联社 Cookie 过期，爬取失败**
重新从浏览器复制 `CLS_TOKEN`、`CLS_UID`、`CLS_COOKIE` 并填入 `sentiment_scraper/cls_crawler.py` 顶部，有效期约 2 个月。

**Q：cninfo 爬取结果为 0 条**
先运行诊断脚本确认接口连通：`python relation_news/diagnose_cninfo.py`

**Q：LLM 返回格式错误**
`news_to_json.py` 内置容错解析，若仍失败会跳过该条并记录日志，不影响其他条目。失败条目不会写入缓存，下次运行会自动重试。

**Q：金融界正文内容很短**
金融界页面为客户端渲染（CSR），正文来自 `<meta name="description">`，是截断的摘要片段，属正常现象，不影响结构化抽取。

**Q：`event_id` 重复**
`event_id` 由 `{股票代码}_{日期}_{当日序号}` 组成，序号在单次运行内按日期独立计数。不同来源的输出分别存储到不同子目录，不会跨文件冲突。