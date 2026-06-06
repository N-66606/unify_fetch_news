# -*- coding: utf-8 -*-
"""
舆情/公告 CSV -> 结构化 JSON（统一版）

支持来源：
  cninfo    巨潮资讯网（公司公告）
  cnstock   中国证券网
  jrj       金融界
  eastmoney 东方财富
  cls       财联社

所有来源的 CSV 均使用统一字段（由各爬虫保证输出对齐）：
  公司 / 关系大类 / 命中关键词 / 来源平台 / 新闻标题 / 媒体 / 发布日期 / 链接 / 摘要 / 正文
  cninfo 额外有：对手方实体 / announcementId

输出：output/{source}_json/舆情_{source}_{公司名}.json（每家公司一个 JSON 数组）
缓存：intermediate/cache/{source}_cache_{公司名}.jsonl

用法：
  python news_to_json.py --source cninfo
  python news_to_json.py --source cnstock --company 立讯精密 --date-start 2026-01-01

环境变量：DASHSCOPE_API_KEY=sk-xxxx
"""

import os, re, csv, glob, json, time, hashlib, logging, argparse, sys
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (COMPANY_FULLNAME, COMPANY_CODE, RELATED_BLACKLIST,
                    MODEL, API_URL, DEFAULT_COMPANIES)

# ============================ 配置 ============================

# 获取项目根目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

API_KEY = os.environ.get("DASHSCOPE_API_KEY", "sk-462681d856674754adae2ea6fe4de16f")
CONTENT_MAXLEN   = 5000
REQUEST_INTERVAL = 0.8

SOURCE_CONFIG = {
    "cninfo": {
        "csv_path":      os.path.join("intermediate", "csv", "result_cninfo.csv"),
        "csv_format":    "relation",
        "source_label":  "巨潮资讯网",
        "output_dir":    os.path.join("output", "cninfo_json"),
        "cache_prefix":  "cninfo",
        "blacklist_extra": [],
        "is_announcement": True,
    },
    "cnstock": {
        "csv_path":      os.path.join("intermediate", "csv", "result_cnstock.csv"),
        "csv_format":    "relation",
        "source_label":  "中国证券网",
        "output_dir":    os.path.join("output", "cnstock_json"),
        "cache_prefix":  "cnstock",
        "blacklist_extra": ["中国证券网", "中证网"],
        "is_announcement": False,
    },
    "jrj": {
        "csv_path":      os.path.join("intermediate", "csv", "result_jrj.csv"),
        "csv_format":    "relation",
        "source_label":  "金融界",
        "output_dir":    os.path.join("output", "jrj_json"),
        "cache_prefix":  "jrj",
        "blacklist_extra": ["金融界"],
        "is_announcement": False,
    },
    "eastmoney": {
        "csv_glob":      os.path.join(PROJECT_ROOT, "sentiment_scraper", "exports", "东方财富", "*.csv"),
        "csv_format":    "sentiment",
        "source_label":  "东方财富",
        "output_dir":    os.path.join("output", "eastmoney_json"),
        "cache_prefix":  "eastmoney",
        "blacklist_extra": [],
        "is_announcement": False,
    },
    "cls": {
        "csv_glob":      os.path.join(PROJECT_ROOT, "sentiment_scraper", "exports", "财联社", "*.csv"),
        "csv_format":    "sentiment",
        "source_label":  "财联社",
        "output_dir":    os.path.join("output", "cls_json"),
        "cache_prefix":  "cls",
        "blacklist_extra": [],
        "is_announcement": False,
    },
}

CACHE_DIR = os.path.join("intermediate", "cache")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("news_to_json")

# ============================ 提示词 ============================

# event_type 完整枚举（无"其他"兜底）：
#   资本运作 / 投资并购 / 关联交易 / 对外担保 / 诉讼仲裁 / 监管处罚 / 股权变动 / 公司治理
#   业务合作 / 供应链 / 竞争动态 / 经营动态 / 研报观点 / 行业资讯

SYSTEM_PROMPT_TEMPLATE = """你是金融领域的上市公司信息结构化抽取助手。
我会给你一条来自财经媒体或上市公司公告的文本（含：锚点公司、标题、正文内容、来源平台）。
{content_note}
请**只依据给定内容**抽取字段，以**严格 JSON 对象**输出，不要任何解释、不要 markdown 代码块、不要多余字段。

输出格式：
{{
 "event_type": "一级事件类型",
 "event_subtype": "二级事件类型",
 "event_time": "YYYY-MM-DD（事件实际发生/签署日期；找不到则返回空字符串）",
 "company_role": "锚点公司在该事件中的角色",
 "related_companies": [{{"company_name": "公司名称", "role": "其在事件中的角色"}}],
 "summary": "一句话中文摘要（不超过60字）",
 "keywords": ["关键词1", "关键词2"],
 "sentiment": "positive 或 neutral 或 negative",
 "importance": 1到5的整数
}}

━━━ event_type（必须从下列14项中选一个，不得使用未列出的类型）━━━

【资本运作】上市公司公告中的股权收购、资产收购、增资、重大资产重组、吸收合并、分拆上市、要约收购、设立子公司。
【投资并购】新闻中报道的收购动态、战略投资、入股、并购意向（含未完成/传闻阶段）。
【关联交易】关联方交易、关联采购/销售/租赁/资产转让。
【对外担保】为子公司或关联方提供担保、互保、担保额度预计。
【诉讼仲裁】民事/行政/商事诉讼、仲裁、法院判决、执行案件。
【监管处罚】证监会/证监局行政处罚、反垄断处罚、市场监管总局处罚、立案调查、警示函。
【股权变动】股权质押、解除质押、大股东增持/减持、股份回购、权益变动披露。
【公司治理】董事会决议、高管变动、章程修改、利润分配、审计报告。
【业务合作】战略合作协议签署、合资设立、联合研发、技术授权、签约合作。
【供应链】供应商/客户关系披露、大额订单获得、供货合同签署。
【竞争动态】竞争格局分析、市场份额变化、竞争对手动态。
【经营动态】业绩说明、股东会表态、经营策略、产能扩张、融资计划（非公告类）。
【研报观点】券商研报、分析师评级、行业研究报告。
【行业资讯】行业政策、市场趋势、宏观经济对行业影响；锚点公司只是被顺带提及的综合资讯也归入此类。

━━━ event_subtype（在 event_type 范围内选更具体的词）━━━

资本运作：股权收购 / 资产收购 / 增资入股 / 重大资产重组 / 吸收合并 / 分拆 / 要约收购 / 对外投资 / 设立子公司
投资并购：股权收购 / 资产收购 / 战略投资 / 增持 / 举牌 / 意向收购
关联交易：日常关联交易 / 关联采购 / 关联销售 / 关联资产转让 / 关联租赁
对外担保：为子公司担保 / 为关联方担保 / 担保额度预计 / 互保
诉讼仲裁：民事诉讼 / 商事仲裁 / 行政诉讼 / 执行案件
监管处罚：行政处罚 / 反垄断处罚 / 立案调查 / 警示函
股权变动：股权质押 / 解除质押 / 大股东增持 / 大股东减持 / 股份回购 / 权益变动
公司治理：董事会决议 / 高管变动 / 利润分配 / 审计报告
业务合作：战略合作 / 合资设立 / 联合研发 / 技术授权 / 签约合作
供应链：获得订单 / 供应商关系 / 客户关系 / 供货合同
竞争动态：竞争格局 / 市场份额 / 竞争对手分析
经营动态：业绩说明 / 股东会表态 / 经营策略 / 产能扩张
研报观点：个股研报 / 行业研报 / 评级调整
行业资讯：行业政策 / 市场动态 / 综合资讯

━━━ 其他字段规范 ━━━

company_role：
  公告类 → 主体（或具体角色：收购方/出售方/担保方/被担保方/关联交易方/原告/被告/被处罚方）
  新闻类 → 主体/供应商/客户/合作方/被提及方

related_companies：
  1. 只列对关系图有价值的企业（交易对手、合作方、竞争对手、监管机构等）
  2. 剔除证券登记结算机构、证券交易所等纯基础设施
  3. 监管处罚/诉讼中的监管机构（证监会、市场监管总局等）应保留，role 填"监管方"
  4. 行业资讯/综合资讯中其他公司不必逐一列出，返回 []
  5. 不要列入锚点公司自身

sentiment：
  positive：获得订单/合同、达成合作、完成扩张性收购、行业政策利好、被正面报道
  negative：被处罚、被诉讼、遭监管调查、失去客户、资产被冻结、担保/质押存在风险
  neutral：例行公司治理披露、中性研报、行业资讯、股份回购/质押等常规操作

importance：
  5 = 重大资产重组/控制权变动/重大监管处罚/大额收购
  4 = 一般收购/战略合作签署/大额订单/反垄断处罚
  3 = 供应链关系披露/一般担保/日常关联交易/研报有实质观点
  2 = 例行质押解押/增减持公告/行业趋势介绍
  1 = 综合资讯中被顺带提及/例行治理披露

只输出 JSON 对象本身。"""

CONTENT_NOTE_ANNOUNCEMENT = (
    "注意：本条为上市公司正式公告，正文来自PDF全文提取，"
    "可能含表格符号和排版噪声，请忽略格式提取关键信息。"
    "我会额外提供程序预抽取的候选对手方，请以正文为准核对并尽量给出公司全称。"
)

CONTENT_NOTE_NEWS = (
    "注意：正文可能是截断的摘要片段，属正常现象，请依据现有内容尽力抽取。"
    "若标题为综合资讯汇总（如财经早餐、要闻汇总、电报摘要等），"
    "且锚点公司只是其中被顺带提及，event_type 应归入“行业资讯”，"
    "event_subtype 填“综合资讯”，company_role 填“被提及方”，"
    "importance=1，related_companies=[]。"
)


def get_system_prompt(is_announcement):
    note = CONTENT_NOTE_ANNOUNCEMENT if is_announcement else CONTENT_NOTE_NEWS
    return SYSTEM_PROMPT_TEMPLATE.format(content_note=note)


def build_user_prompt(rec, source_label, is_announcement):
    content = (rec.get("正文") or rec.get("content") or rec.get("摘要") or "").strip()
    title   = rec.get("新闻标题") or rec.get("title") or ""
    company = rec.get("公司") or rec.get("_company") or ""
    media   = rec.get("媒体") or rec.get("source") or source_label
    pub     = rec.get("发布日期") or rec.get("pub_time") or ""
    cats    = rec.get("关系大类") or ""
    kws     = rec.get("命中关键词") or ""

    lines = [
        f"锚点公司：{company}",
        f"标题：{title}",
        f"来源：{media}",
        f"发布日期：{pub}",
    ]
    if cats:
        lines.append(f"爬虫初判关系大类（仅供参考）：{cats}")
    if kws:
        lines.append(f"命中关键词：{kws}")
    if is_announcement:
        cp = rec.get("对手方实体") or ""
        if cp:
            lines.append(f"候选对手方（程序预抽取，需核对）：{cp}")
    lines.append(f"正文内容：\n{content[:CONTENT_MAXLEN]}")
    return "\n".join(lines)

# ============================ 大模型调用 ============================

def call_llm(rec, source_label, is_announcement, retries=3):
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": get_system_prompt(is_announcement)},
            {"role": "user",   "content": build_user_prompt(rec, source_label, is_announcement)},
        ],
        "temperature": 0.1,
    }
    last_err = None
    for i in range(retries):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            resp_json = r.json()
            # DashScope 部分错误以 HTTP 200 返回，需额外检查
            if "choices" not in resp_json:
                raise ValueError(f"API 返回异常（无 choices 字段）：{str(resp_json)[:200]}")
            text = resp_json["choices"][0]["message"]["content"]
            return _parse_json(text)
        except Exception as e:
            last_err = e
            log.warning(f"模型调用失败(第{i+1}次)：{e}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"模型调用多次失败：{last_err}")


def _parse_json(text):
    if isinstance(text, list):
        text = "".join(seg.get("text", "") for seg in text if isinstance(seg, dict))
    t = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b != -1:
        t = t[a:b + 1]
    result = json.loads(t)
    # 校验必要字段：防止 API 错误响应（HTTP 200 但 body 是错误信息）被当成合法结果缓存
    if not isinstance(result, dict) or "event_type" not in result:
        raise ValueError(f"LLM 返回结构不合法，缺少 event_type，原始内容：{t[:200]}")
    return result

# ============================ 工具函数 ============================

def url_to_id(rec):
    aid = rec.get("announcementId") or ""
    if aid:
        return f"aid_{aid}"
    url = rec.get("链接") or rec.get("url") or rec.get("新闻标题") or rec.get("title") or ""
    return hashlib.md5(url.encode()).hexdigest()[:8]


def normalize_date(s):
    if not s:
        return ""
    m = re.match(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", str(s).strip())
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return str(s)[:10]


def build_event(rec, llm, seq_map, source_label, blacklist):
    company_short = rec.get("公司") or rec.get("_company") or ""
    ts_code  = COMPANY_CODE.get(company_short, "")
    code_num = ts_code.split(".")[0] if ts_code else company_short
    raw_date = rec.get("发布日期") or rec.get("pub_time") or ""
    pub_date = normalize_date(raw_date)
    ymd      = pub_date.replace("-", "")
    seq = seq_map.get(ymd, 0) + 1
    seq_map[ymd] = seq
    event_time = (llm.get("event_time") or "").strip() or pub_date
    related = [
        c for c in (llm.get("related_companies") or [])
        if not any(b in (c.get("company_name") or "") for b in blacklist)
    ]
    content = (rec.get("正文") or rec.get("content") or rec.get("摘要") or "").strip()
    title   = rec.get("新闻标题") or rec.get("title") or ""
    url     = rec.get("链接") or rec.get("url") or ""
    media   = rec.get("媒体") or rec.get("source") or source_label
    if media and media != source_label and not media.startswith(source_label):
        source_field = f"{source_label}-{media}"
    else:
        source_field = media or source_label
    return {
        "event_id":          f"{code_num}_{ymd}_{seq:03d}",
        "company_name":      COMPANY_FULLNAME.get(company_short, company_short),
        "ts_code":           ts_code,
        "source":            source_field,
        "pub_time":          f"{pub_date} 00:00:00" if pub_date else "",
        "title":             title,
        "content":           content,
        "url":               url,
        "event_type":        llm.get("event_type", ""),
        "event_subtype":     llm.get("event_subtype", ""),
        "event_time":        event_time,
        "company_role":      llm.get("company_role", "主体"),
        "related_companies": related,
        "summary":           llm.get("summary", ""),
        "keywords":          llm.get("keywords") or [],
        "sentiment":         llm.get("sentiment", "neutral"),
        "importance":        int(llm.get("importance") or 0),
    }

# ============================ CSV 读取 ============================

def _read_csv(path):
    for enc in ["utf-8-sig", "gbk", "gb2312", "utf-8"]:
        try:
            with open(path, encoding=enc) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
        except Exception as e:
            log.error(f"读取 {path} 失败：{e}"); return []
    log.error(f"无法识别编码：{path}"); return []


def _company_from_filename(path):
    return os.path.splitext(os.path.basename(path))[0].split("_")[0]


def load_data(cfg, company_filter=None, date_start=None, date_end=None):
    by_company = {}
    if cfg["csv_format"] == "relation":
        rows = _read_csv(cfg["csv_path"])
        if not rows:
            log.error(f"无数据：{cfg['csv_path']}"); return {}
        log.info(f"读取 {cfg['csv_path']}，共 {len(rows)} 行")
        for r in rows:
            company = r.get("公司", "").strip()
            if not company: continue
            if company_filter and company not in company_filter: continue
            pub = normalize_date(r.get("发布日期", ""))
            if date_start and pub and pub < date_start: continue
            if date_end   and pub and pub > date_end:   continue
            by_company.setdefault(company, []).append(r)
    else:
        files = sorted(glob.glob(cfg["csv_glob"]))
        if not files:
            log.warning(f"未找到文件：{cfg['csv_glob']}"); return {}
        for fpath in files:
            company = _company_from_filename(fpath)
            if company_filter and company not in company_filter: continue
            rows = _read_csv(fpath)
            for r in rows:
                r["_company"] = company
                pub = normalize_date(r.get("pub_time", ""))
                if date_start and pub and pub < date_start: continue
                if date_end   and pub and pub > date_end:   continue
                by_company.setdefault(company, []).append(r)
            log.info(f"  {os.path.basename(fpath)} → {company}，{len(rows)} 行")
    return by_company

# ============================ 缓存 ============================

def _load_cache(prefix, company):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{prefix}_cache_{company}.jsonl")
    cache = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                o = json.loads(line); cache[o["uid"]] = o["llm"]
            except Exception: pass
    return cache, path


def _append_cache(path, uid, llm):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"uid": uid, "llm": llm}, ensure_ascii=False) + "\n")

# ============================ 主流程 ============================

def run(source, company_filter=None, date_start=None, date_end=None):
    if source not in SOURCE_CONFIG:
        raise ValueError(f"未知 source: {source}，可选：{list(SOURCE_CONFIG)}")
    if not API_KEY:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY 环境变量")
    cfg             = SOURCE_CONFIG[source]
    source_label    = cfg["source_label"]
    output_dir      = cfg["output_dir"]
    cache_prefix    = cfg["cache_prefix"]
    is_announcement = cfg.get("is_announcement", False)
    blacklist       = RELATED_BLACKLIST + cfg["blacklist_extra"]
    by_company = load_data(cfg, company_filter, date_start, date_end)
    if not by_company:
        log.warning(f"[{source}] 没有读取到任何数据，退出"); return
    os.makedirs(output_dir, exist_ok=True)
    total_written = 0
    for company, recs in by_company.items():
        cache, cache_path = _load_cache(cache_prefix, company)
        events, seq_map   = [], {}
        recs.sort(key=lambda r: normalize_date(r.get("发布日期") or r.get("pub_time") or ""))
        log.info(f"[{source}][{company}] 共 {len(recs)} 条待处理")
        for rec in recs:
            uid = url_to_id(rec)
            if uid in cache:
                llm = cache[uid]
            else:
                try:
                    llm = call_llm(rec, source_label, is_announcement)
                    _append_cache(cache_path, uid, llm)
                    time.sleep(REQUEST_INTERVAL)
                except Exception as e:
                    title = rec.get("新闻标题") or rec.get("title") or ""
                    log.error(f"[{source}][{company}] '{title[:30]}' 抽取失败，跳过：{e}")
                    continue
            event = build_event(rec, llm, seq_map, source_label, blacklist)
            events.append(event)
            log.info(f"[{source}][{company}] {event['event_id']} "
                     f"{event['event_type']}/{event['event_subtype']} "
                     f"sentiment={event['sentiment']} importance={event['importance']}")
        out_path = os.path.join(output_dir, f"舆情_{source}_{company}.json")
        log.info(f"[{source}][{company}] 准备写入 {len(events)} 条，路径：{os.path.abspath(out_path)}")
        try:
            serialized = json.dumps(events, ensure_ascii=False, indent=2)
            log.info(f"[{source}][{company}] 序列化成功，字节数：{len(serialized.encode('utf-8'))}")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(serialized)
            # 写完立即读回验证
            with open(out_path, "r", encoding="utf-8") as f:
                verify = f.read()
            verify_data = json.loads(verify)
            log.info(f"[{source}][{company}] 验证读回：{len(verify_data)} 条，文件大小：{os.path.getsize(out_path)} 字节")
        except Exception as e:
            log.error(f"[{source}][{company}] 写入或验证失败：{e}", exc_info=True)
        log.info(f"[{source}][{company}] 写出 {len(events)} 条 -> {out_path}")
        total_written += len(events)
    log.info(f"[{source}] 全部完成，共写出 {total_written} 条")

# ============================ CLI ============================

def main():
    parser = argparse.ArgumentParser(description="舆情/公告 CSV -> 结构化 JSON")
    parser.add_argument("--source",     required=True, choices=list(SOURCE_CONFIG))
    parser.add_argument("--company",    nargs="+", default=None)
    parser.add_argument("--date-start", default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--date-end",   default=None, metavar="YYYY-MM-DD")
    args = parser.parse_args()
    run(source=args.source, company_filter=args.company,
        date_start=args.date_start, date_end=args.date_end)

if __name__ == "__main__":
    main()