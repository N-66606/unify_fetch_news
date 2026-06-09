# -*- coding: utf-8 -*-
"""
大类一｜法定披露关系爬虫  ——  数据源：巨潮资讯网（cninfo）

流程：解析机构号 -> 拉全量公告 -> 标题打关系标签 + 黑名单过滤
      -> 对保留的每条下载PDF、抽全文、抽对手方实体
      -> 输出 CSV（含全文，字段与 cnstock/jrj 对齐，可直接送 news_to_json.py）

接口（POST）：
  机构号: http://www.cninfo.com.cn/new/information/topSearch/detailOfQuery
  公告  : http://www.cninfo.com.cn/new/hisAnnouncement/query
  PDF   : http://static.cninfo.com.cn/ + adjunctUrl

用法：
  python cninfo_crawler.py
  python cninfo_crawler.py --date-start 2026-01-01 --date-end 2026-06-05
  python cninfo_crawler.py --company 立讯精密 中信证券 --date-start 2026-01-01

依赖：pip install requests pdfplumber
      （pdfplumber 抽不出时自动回退到 PyMuPDF: pip install pymupdf）
"""

import os
import csv
import sys
import time
import random
import logging
import argparse
from io import BytesIO
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 项目根目录加入 path，以便 import config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DEFAULT_COMPANIES
from extract_util import extract_counterparties, CN_COMPANY

# ============================ 配置区 ============================

# 以下配置可通过 CLI 参数覆盖，也可直接修改默认值
KEYWORD_GROUPS = {
    "股权与控制": ["收购", "股权转让", "股份转让", "增资", "控股", "参股",
                "实际控制人", "一致行动人", "表决权", "设立", "对外投资", "权益变动", "要约"],
    "并购重组":  ["重大资产重组", "吸收合并", "合并", "资产收购",
                "发行股份购买资产", "分拆", "重组", "出售资产", "购买资产"],
    "担保":     ["担保", "互保"],
    "关联交易":  ["关联交易", "关联方", "关联租赁"],
    "诉讼处罚":  ["诉讼", "仲裁", "处罚", "立案", "被执行", "判决"],
}
TITLE_BLACKLIST = ["管理制度", "管理办法", "议事规则", "工作制度", "工作细则",
                   "实施细则", "管理细则", "工作规则", "登记备案", "管理规定"]
BODY_SKIP_WORDS = ["质押", "增持", "减持", "股东会", "股东大会", "激励", "回购", "法律意见"]

KEEP_ONLY_MATCHED = True
FETCH_FULLTEXT    = True
SAVE_PDF          = False
PAGE_SIZE         = 30
REQUEST_INTERVAL  = (1.0, 2.0)

INTER_DIR  = os.path.join("intermediate")
TEXT_DIR   = os.path.join(INTER_DIR, "text")
PDF_DIR    = os.path.join(INTER_DIR, "pdf")
OUTPUT_CSV_DIR = os.path.join(INTER_DIR, "csv", "cninfo")

# CSV 输出字段（与 cnstock/jrj 对齐，额外保留公告特有字段）
CSV_FIELDS = [
    "公司", "股票代码", "关系大类", "命中关键词", "来源平台",
    "新闻标题",   # 对应公告标题（字段名统一）
    "媒体",       # 固定填"巨潮资讯网"
    "发布日期",   # 对应公告日期
    "链接",       # 对应PDF地址
    "摘要",       # 留空（公告无摘要）
    "正文",       # 全文内容
    # 公告专有字段（供对手方抽取参考，转json时使用）
    "对手方实体", "正文字数", "announcementId",
]

# ============================================================================

CNINFO_BASE  = "http://www.cninfo.com.cn"
URL_ORG      = f"{CNINFO_BASE}/new/information/topSearch/detailOfQuery"
URL_QUERY    = f"{CNINFO_BASE}/new/hisAnnouncement/query"
STATIC_BASE  = "http://static.cninfo.com.cn/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cninfo")


def build_session():
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1.0,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def req_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
        "Origin": "http://www.cninfo.com.cn",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def polite_sleep():
    time.sleep(random.uniform(*REQUEST_INTERVAL))


def resolve_org(session, company_name):
    r = session.post(URL_ORG,
                     data={"keyWord": company_name, "maxSecNum": 10, "maxListNum": 5},
                     headers=req_headers(), timeout=15)
    r.raise_for_status()
    lst = r.json().get("keyBoardList", [])
    if not lst:
        raise ValueError(f"未找到公司：{company_name}")
    hit = next((x for x in lst if x.get("zwjc") == company_name), lst[0])
    code, org_id = hit["code"], hit["orgId"]
    column = "sse" if code[0] == "6" else ("bj" if code[0] in ("4", "8") else "szse")
    log.info(f"{company_name} -> code={code}, orgId={org_id}, column={column}")
    return code, org_id, column


def fetch_all_announcements(session, code, org_id, column, date_start, date_end):
    page, all_anns = 1, []
    while True:
        data = {
            "pageNum": page, "pageSize": PAGE_SIZE, "column": column, "tabName": "fulltext",
            "plate": "", "stock": f"{code},{org_id}", "searchkey": "", "secid": "",
            "category": "", "trade": "",
            "seDate": f"{date_start}~{date_end}",
            "sortName": "time", "sortType": "desc", "isHLtitle": "true",
        }
        r = session.post(URL_QUERY, data=data, headers=req_headers(), timeout=20)
        r.raise_for_status()
        resp = r.json()
        anns  = resp.get("announcements") or []
        total = resp.get("totalRecordNum", 0)
        if page == 1:
            log.info(f"时间段 {date_start}~{date_end} 公告总数：{total}")
        all_anns.extend(anns)
        if not anns or len(all_anns) >= total or not resp.get("hasMore", page * PAGE_SIZE < total):
            break
        page += 1
        polite_sleep()
    return all_anns


def tag_categories(title):
    cats, kws = [], []
    for cat, words in KEYWORD_GROUPS.items():
        hit = [w for w in words if w in title]
        if hit:
            cats.append(cat)
            kws.extend(hit)
    return cats, kws


def is_blacklisted(title):
    return any(b in title for b in TITLE_BLACKLIST)


def pdf_to_text(pdf_bytes):
    """优先 pdfplumber，失败回退 PyMuPDF。"""
    text = ""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
                for tbl in (page.extract_tables() or []):
                    for row in tbl:
                        cells = [c for c in row if c]
                        if cells:
                            parts.append(" ".join(cells))
        text = "\n".join(parts).strip()
    except Exception as e:
        log.debug(f"pdfplumber 失败，尝试 PyMuPDF：{e}")
    if not text:
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            text = "\n".join(p.get_text() for p in doc).strip()
        except Exception as e:
            log.warning(f"PyMuPDF 也失败：{e}")
    return text


def download_bytes(session, url):
    r = session.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=40)
    r.raise_for_status()
    return r.content


def crawl_company(session, company_name, date_start, date_end):
    code, org_id, column = resolve_org(session, company_name)
    polite_sleep()
    anns = fetch_all_announcements(session, code, org_id, column, date_start, date_end)

    # 动态识别主体全称（用于排除自身）
    subject_names = [company_name]
    for a in anns:
        for m in CN_COMPANY.finditer(a.get("announcementTitle") or ""):
            if company_name[:2] in m.group():
                subject_names.append(m.group())
                break
        if len(subject_names) > 1:
            break
    subject_names = list(dict.fromkeys(subject_names))
    log.info(f"主体名称（排除用）：{subject_names}")

    records, matched = [], 0
    for a in anns:
        title = (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
        if is_blacklisted(title):
            continue
        cats, kws = tag_categories(title)
        if KEEP_ONLY_MATCHED and not cats:
            continue
        matched += 1

        ts      = a.get("announcementTime")
        pub     = time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts else ""
        aid     = a.get("announcementId", "")
        pdf_url = STATIC_BASE + a.get("adjunctUrl", "") if a.get("adjunctUrl") else ""

        fulltext       = ""
        counterparties = ""

        if FETCH_FULLTEXT and pdf_url:
            try:
                pdf_bytes = download_bytes(session, pdf_url)
                if SAVE_PDF:
                    os.makedirs(PDF_DIR, exist_ok=True)
                    open(os.path.join(PDF_DIR, f"{company_name}_{pub}_{aid}.pdf"), "wb").write(pdf_bytes)
                text = pdf_to_text(pdf_bytes)
                if text:
                    os.makedirs(TEXT_DIR, exist_ok=True)
                    tf = os.path.join(TEXT_DIR, f"{company_name}_{pub}_{aid}.txt")
                    open(tf, "w", encoding="utf-8").write(text)
                    parties        = extract_counterparties(text, subject_names)
                    fulltext       = text
                    counterparties = "|".join(parties)
                    log.info(f"  [{pub}] {title[:24]}… 正文{len(text)}字 对手方:{parties[:3]}")
                else:
                    fulltext = "(空/疑似扫描件)"
                    log.warning(f"  [{pub}] {title[:24]}… 正文为空，可能是扫描件")
            except Exception as e:
                log.warning(f"  正文获取失败 {pdf_url}：{e}")
            polite_sleep()

        records.append({
            "公司":          company_name,
            "股票代码":      code,
            "关系大类":      "|".join(cats),
            "命中关键词":    "|".join(sorted(set(kws))),
            "来源平台":      "cninfo",
            "新闻标题":      title,       # 公告标题，统一用新闻标题字段
            "媒体":          "巨潮资讯网",
            "发布日期":      pub,
            "链接":          pdf_url,
            "摘要":          "",          # 公告无摘要
            "正文":          fulltext,
            "对手方实体":    counterparties,
            "正文字数":      len(fulltext),
            "announcementId": aid,
        })

    log.info(f"{company_name}：拉取 {len(anns)} 条，保留命中 {matched} 条")
    return records


def main():
    parser = argparse.ArgumentParser(description="巨潮资讯网公告爬虫")
    parser.add_argument("--company",    nargs="+", default=None,
                        help="指定公司（可多个），不填则处理全部")
    parser.add_argument("--date-start", default=None, metavar="YYYY-MM-DD",
                        help="起始日期（含），默认近一年")
    parser.add_argument("--date-end",   default=None, metavar="YYYY-MM-DD",
                        help="截止日期（含），默认今天")
    args = parser.parse_args()

    date_end   = args.date_end   or date.today().strftime("%Y-%m-%d")
    date_start = args.date_start or (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    companies  = args.company or DEFAULT_COMPANIES

    log.info(f"爬取公司：{companies}")
    log.info(f"时间段：{date_start} ~ {date_end}")

    session = build_session()
    total = 0
    for name in companies:
        try:
            recs = crawl_company(session, name, date_start, date_end)
        except Exception as e:
            log.error(f"公司 {name} 处理失败：{e}")
            continue
        if not recs:
            log.info(f"{name}：无命中记录，跳过写入")
            continue
        os.makedirs(OUTPUT_CSV_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_CSV_DIR, f"result_cninfo_{name}.csv")
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(recs)
        total += len(recs)
        log.info(f"{name}：{len(recs)} 条 -> {out_path}")

    log.info(f"完成：共 {total} 条，按公司写入 {OUTPUT_CSV_DIR}")


if __name__ == "__main__":
    main()