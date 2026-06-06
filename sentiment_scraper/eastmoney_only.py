"""
东方财富新闻爬虫（独立版）
用法：python eastmoney_only.py --company 立讯精密 --pages 5
"""

import json, logging, re, time, csv, os
from urllib.parse import quote
import requests

KEYWORD  = "立讯精密"   # 默认关键词
PAGES    = 5            # 默认爬几页（每页20条）
INTERVAL = 1.5          # 请求间隔秒数
# OUTPUT   = "exports/东方财富"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT   = os.path.join(PROJECT_ROOT, "sentiment_scraper", "exports", "东方财富")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

sess = requests.Session()
sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
})

def clean(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()

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
                    "_": str(int(time.time()*1000))},
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
    for p in range(1, pages + 1):
        log.info(f"搜索第 {p} 页...")
        items = fetch_list(keyword, page=p, size=20)
        if not items:
            log.info("无更多数据，停止")
            break
        raw.extend(items)
        log.info(f"  本页 {len(items)} 条，累计 {len(raw)} 条")
        time.sleep(INTERVAL)

    # 日期过滤
    if date_start or date_end:
        filtered = []
        for item in raw:
            pub = (item.get("date") or "")[:10]
            if date_start and pub and pub < date_start: continue
            if date_end   and pub and pub > date_end:   continue
            filtered.append(item)
        raw = filtered

    # 按标题去重
    seen, uniq = set(), []
    for item in raw:
        t = clean(item.get("title", ""))
        if t not in seen:
            seen.add(t); uniq.append(item)
    log.info(f"标题去重后：{len(raw)} -> {len(uniq)} 条")

    # 爬全文
    records = []
    for i, item in enumerate(uniq):
        url     = item.get("url", "")
        title   = clean(item.get("title", ""))
        snippet = clean(item.get("content", ""))
        log.info(f"爬全文 {i+1}/{len(uniq)}: {title[:40]}")
        fulltext = fetch_fulltext(url) if url else snippet
        content  = fulltext if fulltext and len(fulltext) > len(snippet) else snippet
        records.append({
            "source":   f"东方财富-{item.get('mediaName','')}",
            "pub_time": item.get("date", ""),
            "title":    title,
            "content":  content,
            "url":      url,
        })
        time.sleep(INTERVAL)

    return records

def save_csv(records, keyword):
    os.makedirs(OUTPUT, exist_ok=True)
    filename = os.path.join(OUTPUT, f"{keyword}_东方财富.csv")
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["source","pub_time","title","content","url"])
        writer.writeheader()
        writer.writerows(records)
    log.info(f"已导出 {len(records)} 条 -> {filename}")
    return filename

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=KEYWORD)
    ap.add_argument("--pages",     type=int, default=PAGES, help="爬取页数（每页20条）")
    ap.add_argument("--date-start", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--date-end",   default=None, metavar="YYYY-MM-DD")
    args = ap.parse_args()

    records = scrape(args.company, pages=args.pages,
                     date_start=args.date_start, date_end=args.date_end)
    save_csv(records, args.company)