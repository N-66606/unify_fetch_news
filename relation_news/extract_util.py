# -*- coding: utf-8 -*-
"""对手方实体抽取：从公告正文里抽 关联方/被担保方/交易对方 等公司实体。
   规则法 + 上下文定位，作为候选字段；后续可用 LLM 精炼。"""
import re

# 强后缀锚定的中文公司名（避免命中"本公司/子公司"等泛称）
CN_COMPANY = re.compile(
    r'[\u4e00-\u9fa5A-Za-z0-9（）\(\)·]{2,40}?'
    r'(?:股份有限公司|有限责任公司|集团有限公司|集团股份有限公司|有限公司|集团)'
)
# 外文实体（Leoni AG / XXX GmbH / XXX Co., Ltd. 等）
EN_COMPANY = re.compile(
    r'[A-Z][A-Za-z0-9&.\-\' ]{1,45}?'
    r'(?:AG|GmbH|N\.V\.|S\.A\.|S\.p\.A\.|Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Co\.,?\s*Ltd\.?)'
)
# 定位标签：标签后面紧跟的往往就是对手方
LABELS = ["被担保方", "被担保人", "担保对象", "关联方", "关联人", "关联交易方",
          "交易对方", "交易对手方", "交易对手", "标的公司", "标的资产", "收购标的",
          "受让方", "转让方", "交易方", "合作方", "供应商", "客户名称"]

LEAD = "向与为对由从在把将和及、，,：:。.；;（(）)\"'“” \t　"
GENERIC = {"本公司", "子公司", "全资子公司", "控股子公司", "母公司", "分公司",
           "该公司", "上市公司", "关联公司", "公司"}


def _clean(name):
    name = name.strip()
    while name and name[0] in "向与为对由从在把将和及":
        name = name[1:]
    return name.strip(LEAD)


def _find(text):
    out = []
    for m in CN_COMPANY.finditer(text):
        out.append(_clean(m.group()))
    for m in EN_COMPANY.finditer(text):
        out.append(m.group().strip())
    return out


def extract_counterparties(text, subject_names, max_n=15):
    """subject_names: 主体公司自身名称(全称/简称)，用于排除自身。"""
    if not text:
        return []
    subj = {s for s in subject_names if s}
    scored = {}  # name -> score（上下文命中权重更高）

    # 1) 上下文定位：标签后 60 字窗口内的公司名，权重高
    for label in LABELS:
        for m in re.finditer(re.escape(label), text):
            window = text[m.end(): m.end() + 80]
            for name in _find(window):
                if name:
                    scored[name] = scored.get(name, 0) + 3
    # 2) 全文兜底，权重低
    for name in _find(text):
        if name:
            scored[name] = scored.get(name, 0) + 1

    # 3) 过滤：去自身、去泛称、去过短
    def keep(n):
        if n in GENERIC or len(n) < 4:
            return False
        if any(s and (s == n or (len(s) >= 4 and s in n)) for s in subj):
            return False
        return True

    ranked = sorted([n for n in scored if keep(n)],
                    key=lambda n: (-scored[n], len(n)))
    # 去重（保序）
    seen, res = set(), []
    for n in ranked:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res[:max_n]


if __name__ == "__main__":
    # 自测：模拟三类正文片段
    subj = ["立讯精密", "立讯精密工业股份有限公司"]
    samples = {
        "关联交易": "本公司及子公司拟与关联方 立讯有限公司、关联人 香港立讯有限公司 "
                 "发生日常关联交易，预计金额 50 亿元。交易对方为上述关联方。",
        "对外担保": "公司拟为全资子公司 立讯电子（东莞）有限公司 及控股子公司 "
                 "江西立讯智造有限公司 提供担保，被担保方均为合并报表范围内子公司。",
        "收购":   "公司拟收购 闻泰科技股份有限公司 部分子公司股权；另收购 Leoni AG "
                 "及其下属全资子公司股权。标的公司经营汽车线束业务。",
    }
    for k, v in samples.items():
        print(f"[{k}] ->", extract_counterparties(v, subj))