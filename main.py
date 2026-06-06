# -*- coding: utf-8 -*-
"""
舆情数据采集主程序

用法：
  python main.py --source <网站> [--company <公司名>...] [--date-start YYYY-MM-DD] [--date-end YYYY-MM-DD]

支持的网站（--source）：
  cninfo      巨潮资讯网（公司公告）
  cnstock     中国证券网
  jrj         金融界
  eastmoney   东方财富
  cls         财联社（爬取需手动运行 Scraper.py，见提示）

示例：
  python main.py --source cnstock --company 立讯精密 中信证券 --date-start 2026-01-01
  python main.py --source cninfo --date-start 2026-03-01 --date-end 2026-06-05
  python main.py --source eastmoney --skip-crawl   # 已有CSV，只转换
  python main.py --source jrj --skip-convert       # 只爬取不转换
"""

import os, sys, argparse, logging, subprocess
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEFAULT_DAYS_BACK

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")

# ── 各 source 对应的爬虫脚本路径 ──────────────────────────────
CRAWLER_SCRIPTS = {
    "cninfo":    os.path.join("relation_news", "cninfo_crawler.py"),
    "cnstock":   os.path.join("relation_news", "cnstock_crawler.py"),
    "jrj":       os.path.join("relation_news", "jrj_crawler.py"),
    "eastmoney": os.path.join("sentiment_scraper", "eastmoney_only.py"),
    "cls":       None,  # 财联社集成在 Scraper.py，需手动运行
}

NEWS_TO_JSON = os.path.join("relation_news", "news_to_json.py")


def run_script(script_path, extra_args=None):
    cmd = [sys.executable, script_path] + (extra_args or [])
    log.info(f"执行：{' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"脚本退出码 {result.returncode}：{script_path}")


def run_crawler(source, companies, date_start, date_end):
    script = CRAWLER_SCRIPTS.get(source)

    if script is None:  # cls
        log.warning(
            "[cls] 财联社爬虫请手动运行：\n"
            "  cd sentiment_scraper && python Scraper.py --mode once\n"
            "运行完成后按 Enter 继续，程序将执行转换步骤。"
        )
        input("手动爬取完成后按 Enter 继续...")
        return

    if not os.path.exists(script):
        raise FileNotFoundError(f"爬虫脚本不存在：{script}")

    # 所有爬虫都支持 --company / --date-start / --date-end
    args = []
    if companies:
        args += ["--company"] + companies
    if date_start:
        args += ["--date-start", date_start]
    if date_end:
        args += ["--date-end", date_end]

    run_script(script, args)


def run_converter(source, companies, date_start, date_end):
    if not os.path.exists(NEWS_TO_JSON):
        raise FileNotFoundError(f"转换脚本不存在：{NEWS_TO_JSON}")
    args = ["--source", source]
    if companies:
        args += ["--company"] + companies
    if date_start:
        args += ["--date-start", date_start]
    if date_end:
        args += ["--date-end", date_end]
    run_script(NEWS_TO_JSON, args)


def resolve_dates(date_start, date_end):
    today = date.today().isoformat()
    if not date_end:
        date_end = today
    if not date_start:
        date_start = (date.today() - timedelta(days=DEFAULT_DAYS_BACK)).isoformat()
    return date_start, date_end


def main():
    parser = argparse.ArgumentParser(
        description="舆情数据采集主程序：爬取 + 结构化转 JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", required=True,
                        choices=list(CRAWLER_SCRIPTS.keys()),
                        help="目标网站/数据源")
    parser.add_argument("--company", nargs="+", default=None, metavar="公司名",
                        help="指定公司（可多个），不填则处理全部10家")
    parser.add_argument("--date-start", default=None, metavar="YYYY-MM-DD",
                        help=f"起始日期（含），默认最近{DEFAULT_DAYS_BACK}天")
    parser.add_argument("--date-end",   default=None, metavar="YYYY-MM-DD",
                        help="截止日期（含），默认今天")
    parser.add_argument("--skip-crawl",   action="store_true", help="跳过爬取，直接转换已有CSV")
    parser.add_argument("--skip-convert", action="store_true", help="只爬取，不执行转换")
    args = parser.parse_args()

    date_start, date_end = resolve_dates(args.date_start, args.date_end)

    log.info("=== 任务开始 ===")
    log.info(f"  来源：{args.source}")
    log.info(f"  公司：{args.company or '全部'}")
    log.info(f"  时间段：{date_start} ~ {date_end}")

    if not args.skip_crawl:
        log.info(f"--- Step 1: 爬取 [{args.source}] ---")
        try:
            run_crawler(args.source, args.company, date_start, date_end)
        except Exception as e:
            log.error(f"爬取失败：{e}"); sys.exit(1)
    else:
        log.info("--- Step 1: 跳过爬取 ---")

    if not args.skip_convert:
        log.info(f"--- Step 2: 转换 [{args.source}] ---")
        try:
            run_converter(args.source, args.company, date_start, date_end)
        except Exception as e:
            log.error(f"转换失败：{e}"); sys.exit(1)
    else:
        log.info("--- Step 2: 跳过转换 ---")

    log.info("=== 全部完成 ===")


if __name__ == "__main__":
    main()