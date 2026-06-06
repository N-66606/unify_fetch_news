# -*- coding: utf-8 -*-
"""
诊断脚本：直接验证巨潮接口对某公司是否有数据（不做任何关键词过滤）。
跑法：python diagnose_cninfo.py
预期：能看到"公告总数 = 几十~几百"，并打印前 10 条标题。
若这里能出数据 => 说明接口正常，0 条问题出在过滤逻辑。
"""
import requests, time

NAME = "立讯精密"
S = requests.Session()
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# 1) 解析机构号
r = S.post("http://www.cninfo.com.cn/new/information/topSearch/detailOfQuery",
           data={"keyWord": NAME, "maxSecNum": 10, "maxListNum": 5}, headers=H, timeout=15)
item = r.json()["keyBoardList"][0]
code, org = item["code"], item["orgId"]
column = "sse" if code[0] == "6" else ("bj" if code[0] in ("4", "8") else "szse")
print(f"{NAME}: code={code}, orgId={org}, column={column}")

# 2) 只按 stock + 时间区间拉公告（不传 searchkey）
end = time.strftime("%Y-%m-%d")
start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 365 * 86400))
data = {
    "pageNum": 1, "pageSize": 30, "column": column, "tabName": "fulltext",
    "plate": "", "stock": f"{code},{org}", "searchkey": "", "secid": "",
    "category": "", "trade": "", "seDate": f"{start}~{end}",
    "sortName": "time", "sortType": "desc", "isHLtitle": "true",
}
resp = S.post("http://www.cninfo.com.cn/new/hisAnnouncement/query",
              data=data, headers=H, timeout=20).json()
print("公告总数 totalRecordNum =", resp.get("totalRecordNum"))
print("本页返回条数 =", len(resp.get("announcements") or []))
print("---- 前 10 条标题 ----")
for a in (resp.get("announcements") or [])[:10]:
    print(" •", (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""))