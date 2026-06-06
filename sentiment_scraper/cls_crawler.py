# -*- coding: utf-8 -*-
"""
财联社新闻爬虫（独立版）
用法：python cls_crawler.py --company 立讯精密 --date-start 2026-03-01
"""

import hashlib, json, logging, re, time, csv, os
from datetime import datetime
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── 配置 ────────────────────────────────────────────────────
CLS_TOKEN  = "o7BTOi0Gp3Z43Fos6pgpZ1L0qECOA3775216779"
CLS_UID    = "5216779"
CLS_COOKIE = """
HWWAFSESID=82a9f9d0450f1e7410; HWWAFSESTIME=1780213616977; hasTelegraphSound=off; hasTelegraphRemind=off; Hm_lvt_fa5455bb5e9f0f260c32a1d45603ba3e=1780213621; HMACCOUNT=F7535D0338AE45AD; hasTelegraphNotification=off; vipNotificationState=off; _c_WBKFRo=Lw15IsoVeBXvkIvHRmfSsbp4w0MnoRWk9FjjPXXX; _nb_ioWEgULi=; userInfo=%7B%22uid%22%3A5216779%2C%22uname%22%3A%22cls-uiwb94%22%2C%22avatar%22%3A%22https%3A%2F%2Fimage.cls.cn%2Fcailianpress%2Favatar%2F20230309%2Fcailianpress0337098980.png%22%2C%22city%22%3A%22%22%2C%22oauth_info%22%3A%7B%22token%22%3A%22o7BTOi0Gp3Z43Fos6pgpZ1L0qECOA3775216779%22%2C%22lifetime%22%3A1785225847%7D%7D; wafatcltime=2967023; wafatcltoken=5c329464665d3525426f810375ff87d1; Hm_lpvt_fa5455bb5e9f0f260c32a1d45603ba3e=1780217429
"""

REQUEST_INTERVAL = 2

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT       = os.path.join(PROJECT_ROOT, "sentiment_scraper", "exports", "财联社")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


# ── 工具 ────────────────────────────────────────────────────
def clean(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()

def cookie_clean(s):
    return " ".join(s.split())

def extract_title(content: str) -> str:
    m = re.match(r"^【(.+?)】", content or "")
    return m.group(1) if m else ""

def sign(params: dict) -> str:
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()

def make_session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"])))
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer":         "https://www.cls.cn/telegraph",
    })
    s.headers["Cookie"] = cookie_clean(CLS_COOKIE)
    return s


# ── 财联社 API ───────────────────────────────────────────────
APP = "CailianpressWeb"
OS  = "web"
SV  = "8.7.9"
API_CACHE  = "https://www.cls.cn/api/cache"
SEARCH_URL = "https://www.cls.cn/api/sw"
DETAIL_URL = "https://www.cls.cn/detail/{}"


def _cache_params(name, last_time=None):
    p = {"app": APP, "name": name, "os": OS,
         "sv": SV, "token": CLS_TOKEN, "uid": CLS_UID}
    if last_time:
        p["lastTime"] = str(last_time)
    p["sign"] = sign(p)
    return p

def _search_sign():
    return sign({"app": APP, "os": OS, "sv": SV, "token": CLS_TOKEN, "uid": CLS_UID})


def fetch_stream_page(sess, last_time=None):
    params = _cache_params("telegraphList" if last_time else "telegraph", last_time)
    try:
        r = sess.get(API_CACHE, params=params, timeout=15)
        r.raise_for_status()
        roll = r.json().get("data") or {}
        if isinstance(roll, dict):
            return roll.get("roll_data", []) or roll.get("list", [])
        return []
    except Exception as e:
        log.error(f"[财联社流] {e}")
        return []


def search_page(sess, keyword, page=0, rn=20):
    url_params = {"app": APP, "os": OS, "sv": SV,
                  "token": CLS_TOKEN, "uid": CLS_UID, "sign": _search_sign()}
    body = {"type": "telegram", "keyword": keyword,
            "rn": rn, "page": page, "os": OS, "sv": SV, "app": APP}
    try:
        resp = sess.post(
            SEARCH_URL, params=url_params,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=15,
            headers={"origin": "https://www.cls.cn",
                     "content-type": "application/json;charset=UTF-8",
                     "referer": f"https://www.cls.cn/searchPage?keyword={quote(keyword)}&type=telegram"})
        resp.raise_for_status()
        return resp.json().get("data", {})
    except Exception as e:
        log.error(f"[财联社搜索] {e}")
        return {}


# ── 爬取逻辑 ────────────────────────────────────────────────
def scrape_search(sess, keyword, max_pages=10):
    results, page = [], 0
    while page < max_pages:
        log.info(f"[财联社搜索] keyword={keyword} page={page}")
        data  = search_page(sess, keyword, page=page, rn=20)
        tg    = data.get("telegram", {})
        items = tg.get("data", [])
        total = tg.get("total_num", 0)
        if not items:
            break
        for item in items:
            tid     = str(item.get("id", ""))
            content = clean(item.get("descr", "") or item.get("title", ""))
            title   = clean(item.get("title", "")) or extract_title(content)
            ts      = item.get("time", 0)
            results.append({
                "_id":     f"cls_search_{tid}",
                "source":  "财联社",
                "pub_time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                "title":   title,
                "content": content,
                "url":     DETAIL_URL.format(tid),
            })
        log.info(f"  本页{len(items)}条，累计{len(results)}/{total}")
        if len(results) >= total:
            break
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return results


def scrape_stream(sess, keywords, stream_pages=5):
    results = []
    items = fetch_stream_page(sess)
    log.info(f"[财联社流] 首页 {len(items)} 条")
    last_time = items[-1].get("ctime", int(time.time())) if items else int(time.time())

    def _to_record(item):
        text = " ".join(filter(None, [item.get("title",""), item.get("content",""), item.get("brief","")]))
        if not any(k in text for k in keywords):
            return None
        ts      = item.get("ctime", 0)
        tid     = str(item.get("id", ""))
        content = clean(item.get("content","") or item.get("brief",""))
        title   = clean(item.get("title","")) or extract_title(content)
        return {
            "_id":     f"cls_{tid}",
            "source":  "财联社",
            "pub_time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
            "title":   title,
            "content": content,
            "url":     DETAIL_URL.format(tid),
        }

    for it in items:
        rec = _to_record(it)
        if rec:
            results.append(rec)

    for p in range(1, stream_pages):
        time.sleep(REQUEST_INTERVAL)
        items = fetch_stream_page(sess, last_time)
        log.info(f"[财联社流] 第{p+1}页 {len(items)} 条")
        if not items:
            break
        for it in items:
            rec = _to_record(it)
            if rec:
                results.append(rec)
        last_time = items[-1].get("ctime", last_time - 1)
    return results


def scrape(company, date_start=None, date_end=None, max_pages=10, stream_pages=5):
    """爬取财联社，合并搜索+电报流，日期过滤，去重，返回记录列表"""
    sess = make_session()
    keywords = [company]  # 可扩展为 [company, stock_code]

    # 搜索接口
    search_records = scrape_search(sess, company, max_pages=max_pages)

    # 电报流
    stream_records = scrape_stream(sess, keywords, stream_pages=stream_pages)

    # 合并去重（按 _id）
    seen, all_records = set(), []
    for r in search_records + stream_records:
        if r["_id"] not in seen:
            seen.add(r["_id"])
            all_records.append(r)

    # 日期过滤
    if date_start or date_end:
        filtered = []
        for r in all_records:
            pub = (r.get("pub_time") or "")[:10]
            if date_start and pub and pub < date_start:
                continue
            if date_end and pub and pub > date_end:
                continue
            filtered.append(r)
        all_records = filtered

    # 去掉内部字段 _id，输出字段与 eastmoney_only.py 保持一致
    for r in all_records:
        r.pop("_id", None)

    log.info(f"[财联社] {company} 最终 {len(all_records)} 条")
    return all_records


def save_csv(records, company):
    os.makedirs(OUTPUT, exist_ok=True)
    filename = os.path.join(OUTPUT, f"{company}_财联社.csv")
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "pub_time", "title", "content", "url"])
        writer.writeheader()
        writer.writerows(records)
    log.info(f"已导出 {len(records)} 条 -> {filename}")
    return filename


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config import DEFAULT_COMPANIES

    ap = argparse.ArgumentParser()
    ap.add_argument("--company",    nargs="+", default=DEFAULT_COMPANIES, metavar="公司名")
    ap.add_argument("--date-start", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--date-end",   default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--pages",      type=int, default=10, help="搜索页数")
    ap.add_argument("--stream-pages", type=int, default=5, help="电报流页数")
    args = ap.parse_args()

    for co in args.company:
        records = scrape(co, date_start=args.date_start, date_end=args.date_end,
                         max_pages=args.pages, stream_pages=args.stream_pages)
        save_csv(records, co)