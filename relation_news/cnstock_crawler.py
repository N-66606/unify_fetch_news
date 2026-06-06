# -*- coding: utf-8 -*-
"""
中国证券网 (cs.com.cn) 新闻搜索爬虫

接口：GET https://www.cs.com.cn/mi4-web/tv_news/search_articles
      ?wbId=1&page={页}&limit=10&miContent={公司名}&field=miLtitle&sort=pubDate&pubDateRange=
      （field=miLtitle 表示在标题字段搜索 miContent 这个词 -> 标题命中，较精准）
响应：JSON {"code":0,"msg":"success","page":{"totalPage":N,"currPage":,"list":[...]}}
      每条字段：miLtitle(标题,含<font>高亮) / miOrigin(来源媒体) / miContent(摘要,信息量足够)
                / pubDate("YYYY-MM-DD HH:MM:SS") / mmInfo_web_url(原文链接)
分页：标准 page 自增，到 totalPage 或时间窗口外即停。无鉴权。

正文说明：详情页正文是 JS 渲染（requests 抓不到），但 miContent 摘要已含核心事实，
          直接用作正文，不再抓详情页。

输出：intermediate/csv/result_cnstock.csv（字段同 result_news.csv，可直接合并）
依赖：pip install requests beautifulsoup4 lxml
"""

import os
import re
import csv
import time
import random
import logging
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================ 配置区 ============================

COMPANIES = ["立讯精密", "佰维存储", "中芯国际", "中信证券", "工业富联",
             "郑州煤电", "兆易创新", "京能电力", "蔚蓝锂芯", "恒瑞医药"]
DAYS_BACK = 365
MAX_PAGES = 30
LIMIT = 10
REQUEST_INTERVAL = (1.0, 2.5)
OUTPUT_CSV = os.path.join("intermediate", "csv", "result_cnstock.csv")

KEEP_ONLY_RELATION = False        # False=留全部非噪声新闻(未命中标"其他")；True=只留命中关系词的
DROP_SHUJUBAO = True
FETCH_BODY = True                 # True=抓详情页正文(抓不到自动降级用摘要)；False=只用摘要
DROP_RESEARCH = False             # True=丢弃券商研报观点("XX研报称…")；False=保留并标"研报观点"

RELATION_KEYWORDS = {
    "业务合作": ["合作", "战略合作", "签约", "合资", "联合"],
    "供应链":   ["供应商", "客户", "订单", "供货", "供应"],
    "竞争":     ["竞争对手", "竞争", "对标"],
    "投资并购动态": ["收购", "投资", "入股", "并购", "增持", "举牌"],
    "监管处罚": ["处罚", "被罚", "反垄断", "立案", "诉讼", "经营者集中"],
}
NOISE_TITLE = ["成交额", "成交量", "大宗交易", "龙虎榜", "涨停", "跌停", "换手率",
               "主力资金", "资金流向", "净流入", "净流出", "异动", "创新高", "创新低",
               "融资融券", "北向资金", "封单", "竞价", "盘中", "涨幅", "跌幅"]

# ============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cnstock")

SEARCH_URL = "https://www.cs.com.cn/mi4-web/tv_news/search_articles"
_TAG_RE = re.compile(r"<[^>]+>")


def build_session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.2,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def build_headers():
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cs.com.cn/searchlist.html",
    }


def polite_sleep():
    time.sleep(random.uniform(*REQUEST_INTERVAL))


def clean_text(s):
    return _TAG_RE.sub("", s or "").replace("&nbsp;", " ").strip()


def is_noise(title, source):
    if DROP_SHUJUBAO and source == "数据宝":
        return True
    return any(p in title for p in NOISE_TITLE)


def tag_relations(text):
    return [c for c, kws in RELATION_KEYWORDS.items() if any(k in text for k in kws)]


BODY_SELECTORS = [".Custom_UnionStyle", ".article-content", ".article",
                  ".content", "#content", ".text", ".detail-content"]


def parse_body(html):
    """从详情页/正文文件提取正文；空壳页返回空字符串。"""
    soup = BeautifulSoup(html, "lxml")
    box = None
    for sel in BODY_SELECTORS:
        box = soup.select_one(sel)
        if box:
            break
    if box:
        ps = [p.get_text(" ", strip=True) for p in box.select("p")]
        text = "\n".join(t for t in ps if t) or box.get_text(" ", strip=True)
    else:
        ps = [p.get_text(" ", strip=True) for p in soup.select("p")]
        text = "\n".join(t for t in ps if len(t) > 15 and "中证网声明" not in t and "版权" not in t)
    return text.split("中证网声明")[0].strip()


def fetch_body(session, url):
    """GET 详情页正文（不带缓存头，避免 304 空响应）。抓不到返回空。"""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.cs.com.cn/",
    }
    try:
        r = session.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return parse_body(r.text)
    except Exception as e:
        log.debug(f"正文抓取失败 {url}: {e}")
        return ""


def is_research_opinion(title, source="", sub=""):
    """券商研报观点（"XX研报称…"或来源为中证金牛座/券商栏目），对关系图是噪声。"""
    return ("研报" in title) or (source == "中证金牛座") or (sub == "券商")



def fetch_page(session, keyword, page):
    params = {"wbId": 1, "page": page, "limit": LIMIT, "miContent": keyword,
              "field": "miLtitle", "sort": "pubDate", "pubDateRange": ""}
    r = session.get(SEARCH_URL, params=params, headers=build_headers(), timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        log.warning(f"code={j.get('code')} msg={j.get('msg')}")
        return [], 0
    page_obj = j.get("page", {}) or {}
    return page_obj.get("list", []) or [], page_obj.get("totalPage", 0)


def within_window(pub_date, start):
    if not pub_date:
        return True
    try:
        return datetime.strptime(pub_date[:10], "%Y-%m-%d").date() >= start
    except Exception:
        return True


def crawl_company(session, company_name, date_end=None):
    start = date.today() - timedelta(days=DAYS_BACK)
    rows, seen = [], set()

    total_page = None
    for page in range(1, MAX_PAGES + 1):
        try:
            items, tp = fetch_page(session, company_name, page)
        except Exception as e:
            log.warning(f"[{company_name}] page={page} 失败：{e}")
            break
        if total_page is None:
            total_page = tp
            log.info(f"{company_name}：共 {tp} 页")
        if not items:
            break

        all_out = True
        for it in items:
            title = clean_text(it.get("miLtitle") or it.get("richTitle"))
            url = it.get("mmInfo_web_url", "")
            source = it.get("miOrigin", "中国证券网") or "中国证券网"
            pub = (it.get("pubDate") or "")[:10]
            summary = clean_text(it.get("miContent"))

            if within_window(pub, start) and (not date_end or pub <= date_end):
                all_out = False
            else:
                continue
            if not url or url in seen:
                continue
            if is_noise(title, source):
                continue
            if is_research_opinion(title, source, it.get("subNm", "")):
                if DROP_RESEARCH:
                    continue
                cats = ["研报观点"]
            else:
                cats = tag_relations(title + " " + summary)
            if KEEP_ONLY_RELATION and not cats:
                continue
            seen.add(url)

            # 正文：抓详情页；正文与摘要取更长的那个（避免短正文或空摘要导致内容丢失）
            body = ""
            if FETCH_BODY:
                body = fetch_body(session, url)
                polite_sleep()
            content = body if len(body) >= len(summary) else summary

            kws = [k for kws in RELATION_KEYWORDS.values() for k in kws if k in (title + summary)]
            rows.append({
                "公司": company_name,
                "关系大类": "|".join(cats) if cats else "其他",
                "命中关键词": "|".join(sorted(set(kws))),
                "来源平台": "cnstock",
                "新闻标题": title,
                "媒体": source,
                "发布日期": pub,
                "链接": url,
                "摘要": summary,
                "正文": content,
            })

        if all_out:                       # 整页都超出时间窗口（按时间倒序）-> 停
            break
        if total_page and page >= total_page:
            break
        polite_sleep()

    log.info(f"{company_name}：保留 {len(rows)} 条")
    return rows


def main():
    import argparse
    parser = argparse.ArgumentParser(description='中国证券网新闻爬虫')
    parser.add_argument('--company',    nargs='+', default=None)
    parser.add_argument('--date-start', default=None, metavar='YYYY-MM-DD',
                        help='起始日期（含），默认近365天')
    parser.add_argument('--date-end',   default=None, metavar='YYYY-MM-DD',
                        help='截止日期（含），默认今天')
    args = parser.parse_args()

    # 将 CLI 参数转换为爬虫使用的 start date
    global DAYS_BACK
    if args.date_start:
        from datetime import datetime
        delta = date.today() - datetime.strptime(args.date_start, '%Y-%m-%d').date()
        DAYS_BACK = delta.days
    # date_end 过滤在 within_window 中处理（新增上界）
    date_end_filter = args.date_end  # 传入 crawl_company

    targets = args.company or COMPANIES
    session = build_session()
    all_rows = []
    for name in targets:
        all_rows.extend(crawl_company(session, name, date_end_filter))

    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    fields = ["公司", "关系大类", "命中关键词", "来源平台", "新闻标题",
              "媒体", "发布日期", "链接", "摘要", "正文"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)
    log.info(f"完成，共 {len(all_rows)} 条 -> {OUTPUT_CSV}")


if __name__ == "__main__":
    main()