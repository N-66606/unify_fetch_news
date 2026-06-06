"""
舆情数据可视化与分析
依赖: pip install pandas matplotlib wordcloud jieba
"""

import sqlite3
import json
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = "sentiment_news.db"
plt.rcParams["font.sans-serif"] = ["SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def load_data(db_path: str = DB_PATH, company: str = None, days: int = 30):
    conn = sqlite3.connect(db_path)
    query = "SELECT source, company, title, content, pub_time, url FROM news"
    conds, vals = [], []
    if company:
        conds.append("company=?"); vals.append(company)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conds.append("pub_time>=?"); vals.append(since)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY pub_time ASC"
    df = pd.read_sql_query(query, conn, params=vals)
    conn.close()
    df["pub_time"] = pd.to_datetime(df["pub_time"])
    return df


def plot_timeline(df: pd.DataFrame, company: str, output: str = "timeline.png"):
    """新闻数量按天折线图"""
    daily = df.groupby([df["pub_time"].dt.date, "source"]).size().unstack(fill_value=0)
    fig, ax = plt.subplots(figsize=(12, 5))
    daily.plot(ax=ax, marker="o")
    ax.set_title(f"{company} 舆情新闻数量趋势", fontsize=14)
    ax.set_xlabel("日期")
    ax.set_ylabel("条数")
    ax.legend(title="来源")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"时间线图已保存: {output}")


def plot_source_pie(df: pd.DataFrame, company: str, output: str = "source_pie.png"):
    """来源占比饼图"""
    counts = df["source"].value_counts()
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.pie(counts, labels=counts.index, autopct="%1.1f%%", startangle=140)
    ax.set_title(f"{company} 新闻来源分布")
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"来源分布图已保存: {output}")


def generate_report(company: str = "立讯精密", days: int = 7):
    df = load_data(company=company, days=days)
    if df.empty:
        print(f"暂无 {company} 的新闻数据，请先运行 scraper.py")
        return

    print(f"\n{'='*40}")
    print(f"{company} 舆情摘要（最近 {days} 天）")
    print(f"{'='*40}")
    print(f"总计新闻: {len(df)} 条")
    for src, cnt in df["source"].value_counts().items():
        print(f"  - {src}: {cnt} 条")

    print(f"\n--- 最新 10 条 ---")
    latest = df.sort_values("pub_time", ascending=False).head(10)
    for _, row in latest.iterrows():
        text = row["title"] or row["content"][:60]
        print(f"[{row['source']}] {row['pub_time'].strftime('%m-%d %H:%M')} | {text}")

    plot_timeline(df, company, f"{company}_timeline.png")
    plot_source_pie(df, company, f"{company}_source.png")


if __name__ == "__main__":
    generate_report("立讯精密", days=7)