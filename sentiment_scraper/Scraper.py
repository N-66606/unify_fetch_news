"""
财联社 & 东方财富 舆情爬虫 v7

数据来源：
  1. 财联社搜索 /api/sw        → exports/财联社/公司名_天数.csv
  2. 财联社电报流 /api/cache   → 同上合并
  3. 东方财富搜索              → exports/东方财富/公司名_天数.csv
     - 按标题去重
     - 自动爬取原文全文

变更：删除新浪7x24（命中率低）
"""

import hashlib, json, logging, re, sqlite3, time, csv, os
from datetime import datetime, timedelta
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# ★ 配置区
# ============================================================
CLS_TOKEN = "o7BTOi0Gp3Z43Fos6pgpZ1L0qECOA3775216779"
CLS_UID   = "5216779"
CLS_COOKIE = """
HWWAFSESID=82a9f9d0450f1e7410; HWWAFSESTIME=1780213616977; hasTelegraphSound=off; hasTelegraphRemind=off; Hm_lvt_fa5455bb5e9f0f260c32a1d45603ba3e=1780213621; HMACCOUNT=F7535D0338AE45AD; hasTelegraphNotification=off; vipNotificationState=off; _c_WBKFRo=Lw15IsoVeBXvkIvHRmfSsbp4w0MnoRWk9FjjPXXX; _nb_ioWEgULi=; userInfo=%7B%22uid%22%3A5216779%2C%22uname%22%3A%22cls-uiwb94%22%2C%22avatar%22%3A%22https%3A%2F%2Fimage.cls.cn%2Fcailianpress%2Favatar%2F20230309%2Fcailianpress0337098980.png%22%2C%22city%22%3A%22%22%2C%22oauth_info%22%3A%7B%22token%22%3A%22o7BTOi0Gp3Z43Fos6pgpZ1L0qECOA3775216779%22%2C%22lifetime%22%3A1785225847%7D%7D; wafatcltime=2967023; wafatcltoken=5c329464665d3525426f810375ff87d1; Hm_lpvt_fa5455bb5e9f0f260c32a1d45603ba3e=1780217429
"""

COMPANIES = [
    {"name": "立讯精密", "stock_code": "sz002475", "keywords": ["立讯精密", "002475"]},
    {"name": "佰维存储", "stock_code": "sh688525", "keywords": ["佰维存储", "688525"]},
    {"name": "中芯国际", "stock_code": "sh688981", "keywords": ["中芯国际", "688981"]},
    {"name": "中信证券", "stock_code": "sh600030", "keywords": ["中信证券", "600030"]},
    {"name": "工业富联", "stock_code": "sh601138", "keywords": ["工业富联", "601138"]},
    {"name": "郑州煤电", "stock_code": "sh600121", "keywords": ["郑州煤电", "600121"]},
    {"name": "兆易创新", "stock_code": "sh603986", "keywords": ["兆易创新", "603986"]},
    {"name": "京能电力", "stock_code": "sh600791", "keywords": ["京能电力", "600791"]},
    {"name": "蔚蓝锂芯", "stock_code": "sz002245", "keywords": ["蔚蓝锂芯", "002245"]},
    {"name": "恒瑞医药", "stock_code": "sh600276", "keywords": ["恒瑞医药", "600276"]},
]

REQUEST_INTERVAL  = 2
SCHEDULE_INTERVAL = 300
DB_PATH = "sentiment_news.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("scraper.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── 数据库 ───────────────────────────────────────────────────
def init_db(path=DB_PATH):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE IF NOT EXISTS news(
        id TEXT PRIMARY KEY, source TEXT, company TEXT,
        title TEXT, content TEXT, pub_time TEXT, url TEXT,
        raw_json TEXT, created_at TEXT DEFAULT(datetime('now','localtime')))""")
    c.execute("CREATE INDEX IF NOT EXISTS i1 ON news(company)")
    c.execute("CREATE INDEX IF NOT EXISTS i2 ON news(pub_time)")
    c.commit()
    return c

def save_news(conn, rows):
    if not rows: return
    conn.executemany(
        "INSERT OR IGNORE INTO news(id,source,company,title,content,pub_time,url,raw_json)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(r["id"],r["source"],r["company"],r["title"],
          r["content"],r["pub_time"],r["url"],r["raw_json"]) for r in rows])
    conn.commit()
    log.info(f"  -> 写入 {len(rows)} 条")


# ── 工具 ────────────────────────────────────────────────────
def clean(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()

def kw_hit(text, kws):
    return any(k in (text or "") for k in kws)

def cookie_clean(s):
    return " ".join(s.split())

def extract_title(content: str) -> str:
    """从【标题】正文格式中提取标题"""
    m = re.match(r"^【(.+?)】", content or "")
    return m.group(1) if m else ""

def sign(params: dict) -> str:
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()

def make_session(referer: str) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504],
        allowed_methods=["GET"])))
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer":         referer,
    })
    return s

def dedup_by_title(records: list) -> list:
    """按标题去重，保留最早一条"""
    seen, result = set(), []
    for r in records:
        t = r.get("title", "").strip()
        key = t if t else r["id"]   # 无标题则按id
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


# ════════════════════════════════════════════════════════════
# 财联社
# ════════════════════════════════════════════════════════════
class CLS:
    API        = "https://www.cls.cn/api/cache"
    SEARCH_URL = "https://www.cls.cn/api/sw"
    DETAIL     = "https://www.cls.cn/detail/{}"
    APP = "CailianpressWeb"
    OS  = "web"
    SV  = "8.7.9"

    def __init__(self):
        self.sess = make_session("https://www.cls.cn/telegraph")
        self.sess.headers["Cookie"] = cookie_clean(CLS_COOKIE)

    def _cache_params(self, name, last_time=None):
        p = {"app": self.APP, "name": name, "os": self.OS,
             "sv": self.SV, "token": CLS_TOKEN, "uid": CLS_UID}
        if last_time:
            p["lastTime"] = str(last_time)
        p["sign"] = sign(p)
        return p

    def _search_sign(self):
        return sign({"app": self.APP, "os": self.OS, "sv": self.SV,
                     "token": CLS_TOKEN, "uid": CLS_UID})

    # ── 电报流 ───────────────────────────────────────────────
    def _get(self, params):
        try:
            r = self.sess.get(self.API, params=params, timeout=15)
            r.raise_for_status()
            roll = r.json().get("data") or {}
            if isinstance(roll, dict):
                return roll.get("roll_data", []) or roll.get("list", [])
            return []
        except Exception as e:
            log.error(f"[财联社] {e}")
            return []

    def fetch_first(self):
        return self._get(self._cache_params("telegraph"))

    def fetch_page(self, last_time):
        return self._get(self._cache_params("telegraphList", last_time))

    def _stream_record(self, item, company):
        text = " ".join(filter(None, [item.get("title",""), item.get("content",""), item.get("brief","")]))
        if not kw_hit(text, company["keywords"]):
            return None
        ts      = item.get("ctime", 0)
        tid     = str(item.get("id", ""))
        content = clean(item.get("content","") or item.get("brief",""))
        title   = clean(item.get("title","")) or extract_title(content)
        return {
            "id":       f"cls_{tid}",
            "source":   "财联社",
            "company":  company["name"],
            "title":    title,
            "content":  content,
            "pub_time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
            "url":      self.DETAIL.format(tid),
            "raw_json": json.dumps(item, ensure_ascii=False),
        }

    def scrape_stream(self, company, pages=5):
        results = []
        items = self.fetch_first()
        log.info(f"[财联社流] 首页 {len(items)} 条")
        for it in items:
            rec = self._stream_record(it, company)
            if rec: results.append(rec)
        last_time = items[-1].get("ctime", int(time.time())) if items else int(time.time())
        for p in range(1, pages):
            time.sleep(REQUEST_INTERVAL)
            items = self.fetch_page(last_time)
            log.info(f"[财联社流] 第{p+1}页 {len(items)} 条  回溯至:{datetime.fromtimestamp(last_time).strftime('%m-%d %H:%M')}")
            if not items: break
            for it in items:
                rec = self._stream_record(it, company)
                if rec: results.append(rec)
            last_time = items[-1].get("ctime", last_time - 1)
        return results

    # ── 搜索接口 ─────────────────────────────────────────────
    def search(self, keyword, page=0, rn=20):
        url_params = {"app": self.APP, "os": self.OS, "sv": self.SV,
                      "token": CLS_TOKEN, "uid": CLS_UID, "sign": self._search_sign()}
        body = {"type": "telegram", "keyword": keyword,
                "rn": rn, "page": page, "os": self.OS, "sv": self.SV, "app": self.APP}
        try:
            resp = self.sess.post(
                self.SEARCH_URL, params=url_params,
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

    def search_all(self, keyword, max_pages=10):
        results, page = [], 0
        while page < max_pages:
            log.info(f"[财联社搜索] keyword={keyword} page={page}")
            data  = self.search(keyword, page=page, rn=20)
            tg    = data.get("telegram", {})
            items = tg.get("data", [])
            total = tg.get("total_num", 0)
            if not items: break
            results.extend(items)
            log.info(f"  本页{len(items)}条，累计{len(results)}/{total}")
            if len(results) >= total: break
            page += 1
            time.sleep(REQUEST_INTERVAL)
        return results

    def scrape_search(self, company, max_pages=10):
        records = []
        seen_ids = set()
        for kw in company["keywords"][:2]:
            for item in self.search_all(kw, max_pages=max_pages):
                tid = str(item.get("id", ""))
                if tid in seen_ids: continue
                seen_ids.add(tid)
                # descr 是正文全文，title 通常为空
                content   = clean(item.get("descr", "") or item.get("title", ""))
                raw_title = clean(item.get("title", ""))
                title     = raw_title if raw_title else extract_title(content)
                ts        = item.get("time", 0)
                records.append({
                    "id":       f"cls_search_{tid}",
                    "source":   "财联社",
                    "company":  company["name"],
                    "title":    title,
                    "content":  content,
                    "pub_time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                    "url":      self.DETAIL.format(tid),
                    "raw_json": json.dumps(item, ensure_ascii=False),
                })
        return records

    def scrape_all(self, company, max_pages=10, stream_pages=5):
        """财联社全部来源合并，按id去重"""
        rows = self.scrape_search(company, max_pages=max_pages)
        rows += self.scrape_stream(company, pages=stream_pages)
        seen, uniq = set(), []
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"]); uniq.append(r)
        return uniq

    def diagnose(self):
        print("\n--- 财联社诊断 ---")
        items = self.fetch_first()
        if items:
            print(f"✅ 电报流正常，获取 {len(items)} 条")
            print(f"   title={str(items[0].get('title',''))[:80]}")
        else:
            print("❌ 电报流失败")
        print()
        data   = self.search("立讯精密", page=0, rn=5)
        tg     = data.get("telegram", {})
        items2 = tg.get("data", [])
        total  = tg.get("total_num", 0)
        if items2:
            print(f"✅ 搜索接口正常，立讯精密历史共 {total} 条，第1条：")
            print(f"   {str(items2[0].get('descr',''))[:100]}")
        else:
            print("❌ 搜索接口失败")


# ════════════════════════════════════════════════════════════
# 东方财富
# ════════════════════════════════════════════════════════════
class EastMoney:
    EM_URL = "https://search-api-web.eastmoney.com/search/jsonp"

    def __init__(self):
        self.sess = make_session("https://so.eastmoney.com/")

    def fetch_news(self, keyword, page=1, size=20):
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
        params = {
            "cb": "eastmoney_cb",
            "param": json.dumps(param, ensure_ascii=False),
            "_": str(int(time.time() * 1000)),
        }
        try:
            resp = self.sess.get(
                self.EM_URL, params=params, timeout=15,
                headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={quote(keyword)}"})
            resp.raise_for_status()
            text = re.sub(r"^[^(]+\(", "", resp.text).rstrip(");")
            return json.loads(text).get("result", {}).get("cmsArticleWebOld", [])
        except Exception as e:
            log.error(f"[东方财富] {e}")
            return []

    def fetch_fulltext(self, url: str) -> str:
        """爬取东方财富文章原文"""
        try:
            resp = self.sess.get(url, timeout=15,
                                 headers={"Accept": "text/html,application/xhtml+xml,*/*"})
            resp.encoding = resp.apparent_encoding
            # 提取正文：东方财富文章正文在 <div class="newsContent"> 或 <div id="ContentBody">
            for pattern in [
                r'<div[^>]+class="[^"]*newsContent[^"]*"[^>]*>(.*?)</div>',
                r'<div[^>]+id="ContentBody"[^>]*>(.*?)</div>',
                r'<div[^>]+class="[^"]*article[^"]*"[^>]*>(.*?)</div>',
            ]:
                m = re.search(pattern, resp.text, re.DOTALL)
                if m:
                    return clean(m.group(1))
            # fallback：取所有 <p> 标签内容
            paras = re.findall(r"<p[^>]*>(.*?)</p>", resp.text, re.DOTALL)
            text  = " ".join(clean(p) for p in paras if len(clean(p)) > 20)
            return text[:3000] if text else ""
        except Exception as e:
            log.warning(f"[东方财富全文] 爬取失败 {url}: {e}")
            return ""

    def scrape(self, company, pages=10, fetch_full=True):
        keyword = company["keywords"][0]
        raw_records = []

        for p in range(1, pages + 1):
            log.info(f"[东方财富] {company['name']} 第{p}页")
            items = self.fetch_news(keyword, page=p, size=20)
            if not items:
                break
            for item in items:
                iid     = item.get("code", "")
                title   = clean(item.get("title", ""))
                content = clean(item.get("content", ""))   # 摘要，可能不全
                url     = item.get("url", "")
                raw_records.append({
                    "id":       f"eastmoney_{iid}",
                    "source":   f"东方财富-{item.get('mediaName', '')}",
                    "company":  company["name"],
                    "title":    title,
                    "content":  content,
                    "pub_time": item.get("date", ""),
                    "url":      url,
                    "raw_json": json.dumps(item, ensure_ascii=False),
                    "_url":     url,   # 临时字段，用于爬全文
                })
            log.info(f"  获取{len(items)}条")
            time.sleep(REQUEST_INTERVAL)

        # 按标题去重（相同标题只保留第一条）
        before = len(raw_records)
        raw_records = dedup_by_title(raw_records)
        log.info(f"[东方财富] 标题去重：{before} -> {len(raw_records)} 条")

        # 爬取全文（替换摘要）
        if fetch_full:
            for i, rec in enumerate(raw_records):
                url = rec.pop("_url", "")
                if url:
                    log.info(f"[东方财富全文] {i+1}/{len(raw_records)} {url[:60]}")
                    full = self.fetch_fulltext(url)
                    if full and len(full) > len(rec["content"]):
                        rec["content"] = full
                    time.sleep(1)   # 爬全文稍慢一点，礼貌抓取
                else:
                    rec.pop("_url", None)
        else:
            for rec in raw_records:
                rec.pop("_url", None)

        return raw_records

    def diagnose(self):
        print("\n--- 东方财富诊断 ---")
        items = self.fetch_news("立讯精密", page=1, size=3)
        if items:
            print(f"✅ 接口正常，立讯精密第1条：")
            print(f"   title={clean(items[0].get('title',''))[:60]}")
            print(f"   date={items[0].get('date','')}  media={items[0].get('mediaName','')}")
            print(f"   content(摘要)={clean(items[0].get('content',''))[:80]}")
        else:
            print("❌ 接口失败")


# ════════════════════════════════════════════════════════════
# 调度器
# ════════════════════════════════════════════════════════════
class Scheduler:
    def __init__(self, db=DB_PATH):
        self.conn  = init_db(db)
        self.cls   = CLS()
        self.em    = EastMoney()

    def run_once(self, companies=None):
        total = 0
        for co in (companies or COMPANIES):
            log.info(f"\n{'='*40}\n抓取: {co['name']}\n{'='*40}")

            # 财联社（搜索+实时流合并）
            cls_rows = self.cls.scrape_all(co, max_pages=10, stream_pages=5)
            save_news(self.conn, cls_rows)
            log.info(f"[财联社] {co['name']} 入库 {len(cls_rows)} 条")

            # 东方财富（标题去重+爬全文）
            em_rows = self.em.scrape(co, pages=10, fetch_full=True)
            save_news(self.conn, em_rows)
            log.info(f"[东方财富] {co['name']} 入库 {len(em_rows)} 条")

            total += len(cls_rows) + len(em_rows)
            time.sleep(REQUEST_INTERVAL)

        log.info(f"本轮完成，共 {total} 条")
        return total

    def run_forever(self, interval=SCHEDULE_INTERVAL):
        log.info(f"守护模式，间隔 {interval} 秒")
        while True:
            try: self.run_once()
            except Exception as e: log.error(f"异常: {e}", exc_info=True)
            time.sleep(interval)

    def query(self, company, days=7, source_filter=None):
        """
        source_filter: None=全部, 'cls'=仅财联社, 'em'=仅东方财富
        """
        since = (datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        sql   = ("SELECT source,pub_time,title,content,url FROM news "
                 "WHERE company=? AND pub_time>=?")
        params = [company, since]
        if source_filter == "cls":
            sql += " AND source='财联社'"
        elif source_filter == "em":
            sql += " AND source LIKE '东方财富%'"
        sql += " ORDER BY pub_time DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [{"source":r[0],"pub_time":r[1],"title":r[2],"content":r[3],"url":r[4]} for r in rows]

    def export_csv(self, company, days=7):
        """分别导出财联社和东方财富到不同子目录"""
        for src, sf, subdir in [
            ("财联社", "cls", "财联社"),
            ("东方财富", "em",  "东方财富"),
        ]:
            news = self.query(company, days=days, source_filter=sf)
            if not news:
                log.info(f"[{src}] 暂无数据，跳过")
                continue
            dirpath = os.path.join("exports", subdir)
            os.makedirs(dirpath, exist_ok=True)
            filename = os.path.join(dirpath, f"{company}_{days}天.csv")
            with open(filename, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["source","pub_time","title","content","url"])
                writer.writeheader()
                writer.writerows(news)
            print(f"[{src}] 已导出 {len(news)} 条 -> {filename}")

    def diagnose(self):
        self.cls.diagnose()
        self.em.diagnose()


# ════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["once","daemon","query","diagnose"], default="once")
    ap.add_argument("--company", default="立讯精密")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    s = Scheduler()

    if args.mode == "once":
        s.run_once()
    elif args.mode == "daemon":
        s.run_forever()
    elif args.mode == "diagnose":
        s.diagnose()
    elif args.mode == "query":
        s.export_csv(args.company, args.days)