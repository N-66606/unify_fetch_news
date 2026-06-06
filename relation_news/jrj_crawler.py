# -*- coding: utf-8 -*-
"""
金融界 (jrj.com.cn) 新闻搜索爬虫

【接口说明】
搜索列表：POST https://gateway.jrj.com/jrj-news/news/searchNews2025
  body: {"keyWord":"公司名","pageSize":10,"pageNo":N,"type":2,"makeDate":"YYYY-MM-DD HH:MM:SS"}
  注：makeDate 是翻页游标，第1页传""，之后传上一页最后一条的 makeDate

正文获取：金融界 PC/移动端均为 CSR（JS渲染），requests 拿不到 <body>。
  策略：GET pcInfoUrl，从 <head> 里的 <meta name="description"> 提取摘要文本，
        与搜索结果自带的 detail 字段取更长的那个作为正文。
  说明：description meta 由服务端直出（SEO用），通常比 detail 更完整，
        但仍是截断摘要——对知识图谱关系抽取已足够。

输出：intermediate/csv/result_jrj.csv（字段同 result_cnstock.csv，可直接合并）
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
DAYS_BACK  = 365
PAGE_SIZE  = 10
MAX_PAGES  = 30
REQUEST_INTERVAL = (1.2, 2.8)
OUTPUT_CSV = os.path.join("intermediate", "csv", "result_jrj.csv")

# True=抓 pcInfoUrl 提取 meta description（比 detail 通常更完整）
# False=只用搜索结果自带的 detail 字段，速度更快但内容可能略短
FETCH_BODY         = True
KEEP_ONLY_RELATION = False
DROP_RESEARCH      = False  # True=丢弃研报观点；False=保留并标"研报观点"

RELATION_KEYWORDS = {
    "业务合作":     ["合作", "战略合作", "签约", "合资", "联合"],
    "供应链":       ["供应商", "客户", "订单", "供货", "供应"],
    "竞争":         ["竞争对手", "竞争", "对标"],
    "投资并购动态": ["收购", "投资", "入股", "并购", "增持", "举牌"],
    "监管处罚":     ["处罚", "被罚", "反垄断", "立案", "诉讼", "经营者集中"],
}
NOISE_TITLE = ["成交额", "成交量", "大宗交易", "龙虎榜", "涨停", "跌停", "换手率",
               "主力资金", "资金流向", "净流入", "净流出", "异动", "创新高", "创新低",
               "融资融券", "北向资金", "封单", "竞价", "盘中", "涨幅", "跌幅",
               "最强股票", "一图看懂", "周涨幅", "日涨幅"]

# =============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("jrj")

SEARCH_URL = "https://gateway.jrj.com/jrj-news/news/searchNews2025"

SEARCH_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/json",
    "deviceinfo": ('{"productId":"6000021","version":"0.0.0",'
                   '"device":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",'
                   '"sysName":"Chrome","sysVersion":["chrome/148.0.0.0"]}'),
    "origin": "https://www.jrj.com.cn",
    "productid": "6000021",
    "referer": "https://www.jrj.com.cn/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
}

PAGE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.jrj.com.cn/",
}


def build_session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.2,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def polite_sleep():
    time.sleep(random.uniform(*REQUEST_INTERVAL))


def is_noise(title):
    return any(p in title for p in NOISE_TITLE)


def is_research(title, source=""):
    return ("研报" in title) or (source in ("中证金牛座",))


def tag_relations(text):
    return [c for c, kws in RELATION_KEYWORDS.items() if any(k in text for k in kws)]


def fetch_meta_description(session, pc_url):
    """
    GET pcInfoUrl，从 <head> 的 <meta name="description"> 提取正文摘要。
    金融界页面为 CSR，<body> 为空，但 description meta 由服务端直出（SEO），
    通常包含比搜索结果 detail 字段更完整的正文片段。
    失败或内容过短时返回空字符串。
    """
    if not pc_url:
        return ""
    try:
        r = session.get(pc_url, headers=PAGE_HEADERS, timeout=20)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        # 优先取 og:description（有时比 description 更完整）
        for attr in [{"property": "og:description"}, {"name": "description"}]:
            tag = soup.find("meta", attr)
            if tag and tag.get("content", "").strip():
                return tag["content"].strip()
        return ""
    except Exception as e:
        log.debug(f"meta description 获取失败 {pc_url}: {e}")
        return ""


def fetch_page(session, keyword, page_no, cursor_date):
    """
    cursor_date: 第1页传 ""，之后传上一页最后一条的 makeDate
    返回 (items, next_cursor_date)
    """
    body = {
        "keyWord":  keyword,
        "pageSize": PAGE_SIZE,
        "pageNo":   page_no,
        "type":     2,
        "makeDate": cursor_date,
    }
    r = session.post(SEARCH_URL, json=body, headers=SEARCH_HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 20000:
        log.warning(f"搜索返回异常 code={j.get('code')}")
        return [], ""
    items = (j.get("data") or {}).get("data") or []
    next_cursor = items[-1]["makeDate"] if items else ""
    return items, next_cursor


def within_window(make_date_str, start):
    """makeDate 格式：'2026-06-02 18:24:18'"""
    if not make_date_str:
        return True
    try:
        return datetime.strptime(make_date_str[:10], "%Y-%m-%d").date() >= start
    except Exception:
        return True


def crawl_company(session, company_name, date_end=None):
    start = date.today() - timedelta(days=DAYS_BACK)
    rows, seen = [], set()
    cursor_date = ""

    for page in range(1, MAX_PAGES + 1):
        try:
            items, cursor_date = fetch_page(session, company_name, page, cursor_date)
        except Exception as e:
            log.warning(f"[{company_name}] page={page} 失败：{e}")
            break

        if not items:
            log.info(f"[{company_name}] page={page} 无数据，停止")
            break

        log.info(f"[{company_name}] page={page}，本页 {len(items)} 条")

        all_out = True
        for it in items:
            make_date = it.get("makeDate", "")
            title     = (it.get("title") or "").strip()
            pc_url    = it.get("pcInfoUrl", "")
            source    = it.get("paperMediaSource", "金融界") or "金融界"
            # detail 字段是搜索结果自带的摘要
            summary   = (it.get("detail") or it.get("summary") or "").strip()
            pub_date  = make_date[:10] if make_date else ""

            if within_window(make_date, start) and (not date_end or make_date[:10] <= date_end):
                all_out = False
            else:
                continue

            if not pc_url or pc_url in seen:
                continue
            if is_noise(title):
                continue
            if is_research(title, source):
                if DROP_RESEARCH:
                    continue
                cats = ["研报观点"]
            else:
                cats = tag_relations(title + " " + summary)
            if KEEP_ONLY_RELATION and not cats:
                continue

            seen.add(pc_url)

            # 正文：从详情页 <head> 的 meta description 提取，取与 detail 摘要中更长的
            content = summary
            if FETCH_BODY:
                meta_desc = fetch_meta_description(session, pc_url)
                if len(meta_desc) > len(summary):
                    content = meta_desc
                polite_sleep()

            kws = [k for kws in RELATION_KEYWORDS.values()
                   for k in kws if k in (title + " " + summary)]
            rows.append({
                "公司":       company_name,
                "关系大类":   "|".join(cats) if cats else "其他",
                "命中关键词": "|".join(sorted(set(kws))),
                "来源平台":   "jrj",
                "新闻标题":   title,
                "媒体":       source,
                "发布日期":   pub_date,
                "链接":       pc_url,
                "摘要":       summary,
                "正文":       content,
            })

        if all_out:
            log.info(f"[{company_name}] 整页超出时间窗口，停止")
            break

    log.info(f"[{company_name}] 保留 {len(rows)} 条")
    return rows


def main():
    import argparse
    parser = argparse.ArgumentParser(description='金融界新闻爬虫')
    parser.add_argument('--company',    nargs='+', default=None)
    parser.add_argument('--date-start', default=None, metavar='YYYY-MM-DD',
                        help='起始日期（含），默认近365天')
    parser.add_argument('--date-end',   default=None, metavar='YYYY-MM-DD',
                        help='截止日期（含），默认今天')
    args = parser.parse_args()

    global DAYS_BACK
    if args.date_start:
        from datetime import datetime
        delta = date.today() - datetime.strptime(args.date_start, '%Y-%m-%d').date()
        DAYS_BACK = delta.days
    date_end_filter = args.date_end

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