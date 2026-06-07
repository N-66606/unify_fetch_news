"""
东方财富新闻爬虫（独立版）
用法：python eastmoney_only.py --company 立讯精密 --pages 5
用法：python eastmoney_only.py --company 立讯精密 中信证券 --pages 5
"""

import json, logging, re, time, csv, os
from collections import defaultdict
from urllib.parse import quote
import requests

PAGES    = 5            # 默认爬几页（每页20条）
INTERVAL = 1.5          # 请求间隔秒数
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT       = os.path.join(PROJECT_ROOT, "sentiment_scraper", "exports", "东方财富")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

sess = requests.Session()
sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
})

def clean(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ── 噪声过滤 ─────────────────────────────────────────────────
# 过滤对知识图谱无价值的行情数据播报类条目，在爬全文前执行以节省请求数。
# 如需保留某类，注释掉对应行即可。
_NOISE_PATTERNS = [
    r'大宗交易|大宗成交',                           # 大宗交易数据
    r'成交额超\d|成交额超[上一]|成交额达\d',        # 盘中成交额实时播报
    r'龙虎榜',                                       # 龙虎榜席位数据
    r'融资融券|融资买入|融券卖出',                   # 融资融券日报
    r'主力资金|主力净[流买卖入出]|主力吸筹',         # 主力资金流向
    r'股东户数为\d',                                 # 股东户数例行披露
    r'累计回购.*\d+股|回购.*股份.*\d+股',            # 回购进度播报（公告来源已有覆盖）
    r'早参$|早报$|晚报$|要闻$|财经早$|数智早|周报$', # 综合资讯汇总（摘要类，正文价值低）
]
_NOISE_RE = re.compile('|'.join(f'(?:{p})' for p in _NOISE_PATTERNS))

def is_noise(title: str) -> bool:
    """判断是否为无价值的行情/数据播报类条目。"""
    return bool(_NOISE_RE.search(title))


# ── 事件去重（同日同事件多媒体报道合并）────────────────────────
# 东方财富聚合了数十家媒体，同一事件常被多来源重复报道。
# 策略：提取标题中的「数字 + 事件动词」作为事件指纹，同日内指纹有重叠的
#       条目归为同一事件，仅保留来源优先级最高 & 正文最长的一条。
#       无法提取指纹的条目再用关键词 Jaccard 相似度兜底。

_EVENT_VERBS_RE = re.compile(
    r'(罚款?|处罚|违法|诉讼?|起诉|收购|并购|合并|增持|减持|回购|'
    r'合作|订单|担保|质押|仲裁|整改|立案|警告|处分|制裁)'
)
_STOPWORDS = set(
    '立讯精密 佰维存储 中芯国际 中信证券 工业富联 郑州煤电 兆易创新 '
    '京能电力 蔚蓝锂芯 恒瑞医药 '
    '的 了 在 与 及 和 或 对 被 其 等 但 由 因 为 至 从 中 上 下 以 '
    '将 已 有 是 不 无 未 于 之 此 该 其 并 到 向 后 前 内 外 间 次 '
    '约 共 总 达 超 逾 涉 称 指 按'.split()
)

# 来源优先级：越靠前越优先（官方机构 > 专业财经 > 其他）
_SOURCE_PRIORITY = [
    '市场监管总局', '证监会', '发改委', '交易所',
    '财联社', '第一财经', '21世纪经济报道',
    '证券时报', '上海证券报', '中国证券报',
    '每日经济新闻', '界面新闻', '澎湃新闻',
    '证券日报', '中新经纬',
]

def _source_priority(source: str) -> int:
    src = source.replace('东方财富-', '')
    for i, s in enumerate(_SOURCE_PRIORITY):
        if s in src:
            return i
    return len(_SOURCE_PRIORITY)

def _event_tag(title: str):
    """提取事件指纹：数字集合 + 动词集合 → frozenset，无特征返回 None。"""
    nums  = set(re.findall(r'\d+(?:\.\d+)?(?:万|亿|%)?', title))
    verbs = set(_EVENT_VERBS_RE.findall(title))
    if nums and verbs:
        return frozenset(nums | verbs)
    if verbs:           # 无数字但有动词，仍可聚类
        return frozenset(verbs)
    return None

def _tokenize(title: str) -> set:
    """提取标题关键词（用于 Jaccard 兜底）。"""
    chars = re.findall(r'[\u4e00-\u9fa5]{2,6}', title)
    nums  = re.findall(r'\d+(?:\.\d+)?(?:万|亿|%|元|股)?', title)
    return set(chars + nums) - _STOPWORDS

def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def event_dedup(records: list) -> list:
    """
    对爬取结果按事件去重：同日同事件多来源报道只保留最优一条。
    records: [{"source", "pub_time", "title", "content", "url"}, ...]
    返回去重后的列表，保持原始顺序。
    """
    # 按日期分组
    date_groups: dict = defaultdict(list)
    for i, r in enumerate(records):
        d = (r.get('pub_time') or '')[:10]
        date_groups[d].append((i, r))

    kept_indices = set()

    for date, group in date_groups.items():
        # Step 1：按事件指纹分桶
        tag_buckets: dict = defaultdict(list)
        no_tag = []
        for idx, r in group:
            tag = _event_tag(r['title'])
            if tag:
                tag_buckets[tag].append((idx, r))
            else:
                no_tag.append((idx, r))

        # Step 2：Union-Find 合并有重叠指纹的桶
        # （同一事件不同报道可能产生不同但有交集的指纹子集）
        tags = list(tag_buckets.keys())
        parent = {t: t for t in tags}

        def _find(t):
            while parent[t] != t:
                parent[t] = parent[parent[t]]
                t = parent[t]
            return t

        for i in range(len(tags)):
            for j in range(i + 1, len(tags)):
                if tags[i] & tags[j]:
                    parent[_find(tags[i])] = _find(tags[j])

        merged: dict = defaultdict(list)
        for tag, members in tag_buckets.items():
            merged[_find(tag)].extend(members)

        # Step 3：Jaccard 兜底——无指纹条目尝试归入已有聚类
        cluster_kws = {
            root: set().union(*(_tokenize(r['title']) for _, r in members))
            for root, members in merged.items()
        }
        remaining_no_tag = []
        for idx, r in no_tag:
            kws = _tokenize(r['title'])
            best_root = max(cluster_kws, key=lambda rt: _jaccard(kws, cluster_kws[rt]),
                            default=None)
            if best_root and _jaccard(kws, cluster_kws[best_root]) >= 0.20:
                merged[best_root].append((idx, r))
            else:
                remaining_no_tag.append((idx, r))

        # Step 4：每个聚类保留「来源优先级最高，正文最长」的一条
        for root, members in merged.items():
            best_idx, best_r = min(
                members,
                key=lambda x: (_source_priority(x[1]['source']), -len(x[1].get('content') or ''))
            )
            kept_indices.add(best_idx)
            removed = len(members) - 1
            if removed > 0:
                log.info(
                    f"事件去重 [{date}] {len(members)}条→1，"
                    f"保留[{best_r['source'].replace('东方财富-', '')}]"
                    f"《{best_r['title'][:30]}》，丢弃 {removed} 条重复报道"
                )

        for idx, _ in remaining_no_tag:
            kept_indices.add(idx)

    result = [r for i, r in enumerate(records) if i in kept_indices]
    log.info(f"事件去重完成：{len(records)} -> {len(result)} 条（减少 {len(records) - len(result)} 条）")
    return result


# ── 搜索接口 ─────────────────────────────────────────────────
def fetch_list(keyword, page=1, size=20):
    param = {
        "uid": "", "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default",
            "pageIndex": page, "pageSize": size,
            "preTag": "", "postTag": "",
        }}
    }
    try:
        resp = sess.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False),
                    "_": str(int(time.time() * 1000))},
            headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={quote(keyword)}"},
            timeout=15)
        resp.raise_for_status()
        text = re.sub(r"^[^(]+\(", "", resp.text).rstrip(");")
        return json.loads(text).get("result", {}).get("cmsArticleWebOld", [])
    except Exception as e:
        log.error(f"搜索失败: {e}")
        return []


# ── 爬全文 ───────────────────────────────────────────────────
def fetch_fulltext(url):
    try:
        resp = sess.get(url, timeout=15,
                        headers={"Accept": "text/html,application/xhtml+xml,*/*"})
        resp.encoding = resp.apparent_encoding
        # 正文在 id="ContentBody" 内，截到 <!-- 文尾
        m = re.search(r'id="ContentBody"[^>]*>(.*?)</div>\s*<!--\s*文尾', resp.text, re.DOTALL)
        if m:
            return clean(m.group(1)).strip()
        # fallback：拼接所有 <p>
        paras = re.findall(r"<p[^>]*>(.*?)</p>", resp.text, re.DOTALL)
        return " ".join(clean(p) for p in paras if len(clean(p)) > 20)
    except Exception as e:
        log.warning(f"全文爬取失败 {url}: {e}")
        return ""


# ── 主流程 ───────────────────────────────────────────────────
def scrape(keyword, pages=5, date_start=None, date_end=None):
    raw = []
    page = 1
    has_date_filter = date_start or date_end

    while True:
        log.info(f"搜索第 {page} 页...")
        items = fetch_list(keyword, page=page, size=20)
        if not items:
            log.info("无更多数据，停止")
            break

        if has_date_filter:
            page_earliest_date = (items[-1].get("date") or "")[:10]
            if date_start and page_earliest_date and page_earliest_date < date_start:
                log.info(f"当前页最早日期 {page_earliest_date} 早于起始日期 {date_start}，停止爬取")
                raw.extend(items)
                break
            if not date_start and page >= pages:
                log.info(f"已达到最大页数 {pages}，停止爬取")
                raw.extend(items)
                break
        else:
            if page > pages:
                log.info(f"已达到最大页数 {pages}，停止爬取")
                break

        raw.extend(items)
        log.info(f"  本页 {len(items)} 条，累计 {len(raw)} 条")
        page += 1
        time.sleep(INTERVAL)

    # Step 1：日期过滤
    if date_start or date_end:
        filtered = []
        for item in raw:
            pub = (item.get("date") or "")[:10]
            if date_start and pub and pub < date_start: continue
            if date_end   and pub and pub > date_end:   continue
            filtered.append(item)
        raw = filtered
        log.info(f"日期过滤后：{len(raw)} 条")

    # Step 2：噪声过滤（大宗交易/盘中播报/融资融券等行情数据）
    before_noise = len(raw)
    raw = [item for item in raw if not is_noise(clean(item.get("title", "")))]
    log.info(f"噪声过滤后：{before_noise} -> {len(raw)} 条（过滤 {before_noise - len(raw)} 条）")

    # Step 3：标题精确去重
    seen, uniq = set(), []
    for item in raw:
        t = clean(item.get("title", ""))
        if t not in seen:
            seen.add(t)
            uniq.append(item)
    log.info(f"标题去重后：{len(raw)} -> {len(uniq)} 条")

    # Step 4：事件去重（同日同事件多媒体来源合并，在爬全文前执行以节省请求数）
    pre_dedup = [
        {
            "source":   "东方财富-" + item.get("mediaName", ""),
            "pub_time": item.get("date", ""),
            "title":    clean(item.get("title", "")),
            "content":  clean(item.get("content", "")),
            "url":      item.get("url", ""),
        }
        for item in uniq
    ]
    pre_dedup = event_dedup(pre_dedup)

    # Step 5：爬全文
    records = []
    for i, d in enumerate(pre_dedup):
        url     = d["url"]
        title   = d["title"]
        snippet = d["content"]
        log.info(f"爬全文 {i + 1}/{len(pre_dedup)}: {title[:40]}")
        fulltext = fetch_fulltext(url) if url else snippet
        content  = fulltext if fulltext and len(fulltext) > len(snippet) else snippet
        records.append({
            "source":   d["source"],
            "pub_time": d["pub_time"],
            "title":    title,
            "content":  content,
            "url":      url,
        })
        time.sleep(INTERVAL)

    return records


def save_csv(records, keyword):
    os.makedirs(OUTPUT, exist_ok=True)
    filename = os.path.join(OUTPUT, f"{keyword}_东方财富.csv")
    
    # 先保存到临时 JSON 文件（作为备份）
    temp_json = os.path.join(OUTPUT, f"{keyword}_东方财富_temp.json")
    try:
        with open(temp_json, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        log.info(f"已保存临时备份 -> {temp_json}")
    except Exception as e:
        log.warning(f"保存临时备份失败：{e}")
    
    # 写入 CSV
    try:
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["source", "pub_time", "title", "content", "url"])
            writer.writeheader()
            writer.writerows(records)
        log.info(f"已导出 {len(records)} 条 -> {filename}")
        
        # CSV 写入成功，删除临时备份
        if os.path.exists(temp_json):
            os.remove(temp_json)
            log.info(f"已删除临时备份")
        
        return filename
    except PermissionError:
        log.error(f"无法写入文件：{filename}")
        log.error("请关闭可能正在打开此文件的程序（如 Excel、记事本、VS Code 等），然后重试")
        log.error(f"数据已保存到临时文件：{temp_json}")
        log.error("关闭文件后，可以运行以下命令从临时文件恢复：")
        log.error(f"  python -c \"import json, csv; records=json.load(open('{temp_json}', encoding='utf-8')); exec(open('sentiment_scraper/eastmoney_only.py').read().split('def save_csv')[0]); save_csv(records, '{keyword}')\"")
        raise


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", nargs="+", required=True, help="公司名称（可多个）")
    ap.add_argument("--pages",      type=int, default=PAGES, help="爬取页数（每页20条）")
    ap.add_argument("--date-start", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--date-end",   default=None, metavar="YYYY-MM-DD")
    args = ap.parse_args()

    all_records = []
    for company in args.company:
        log.info(f"\n{'=' * 50}")
        log.info(f"开始爬取：{company}")
        log.info(f"{'=' * 50}")
        records = scrape(company, pages=args.pages,
                         date_start=args.date_start, date_end=args.date_end)
        all_records.extend(records)
        save_csv(records, company)

    log.info(f"\n总计爬取 {len(all_records)} 条记录（{len(args.company)} 家公司）")