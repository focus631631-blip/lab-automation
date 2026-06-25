#!/usr/bin/env python3
"""GitHub Actions 版盘前简报 — 交易日 8:55 UTC+8 触发"""
import os, sys, json, re, requests, subprocess
from datetime import datetime

PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
HOLDINGS_JSON = os.environ.get("HOLDINGS", '[{"code":"sh600089","name":"特变电工","weight":70},{"code":"sz002028","name":"思源电气","weight":30}]')

HOLDINGS = json.loads(HOLDINGS_JSON)
US_SYMBOLS = "int_dji,int_nasdaq,int_sp500,gb_baba,gb_pdd,gb_jd,gb_nio,gb_xpev,gb_li,gb_bili"
SEMI_US = "gb_tsm,gb_nvda,gb_asx"
SEMI_HK = "hk00981,hk01347"
A_INDEX = "s_sh000001,s_sz399001,s_sz399006"

US_LABELS = {
    "int_dji":"道指","int_nasdaq":"纳指","int_sp500":"标普",
    "gb_baba":"阿里","gb_pdd":"拼多多","gb_jd":"京东",
    "gb_nio":"蔚来","gb_xpev":"小鹏","gb_li":"理想","gb_bili":"B站",
}
SEMI_LABELS = {"gb_tsm":"台积电","gb_nvda":"英伟达","gb_asx":"日月光",}
SEMI_HK_L = {"hk00981":"中芯国际","hk01347":"华虹半导体",}
A_LABELS = {"s_sh000001":"上证","s_sz399001":"深证","s_sz399006":"创业板"}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_sina(symbols):
    r = subprocess.run(["curl","-s","-m","15",
        f"http://hq.sinajs.cn/list={symbols}",
        "-H","Referer: https://finance.sina.com.cn/"],
        capture_output=True, timeout=20)
    text = r.stdout.decode("gbk", errors="replace")
    data = {}
    for line in text.strip().split("\n"):
        if "hq_str_" not in line or '="' not in line: continue
        try:
            key = line.split("hq_str_")[1].split("=")[0]
            payload = line.split('"',1)[1].rsplit('"',1)[0]
            if payload: data[key] = payload.split(",")
        except Exception: continue
    return data

def fetch_us_concepts():
    raw = fetch_sina(US_SYMBOLS)
    out = []
    for key, label in US_LABELS.items():
        if key not in raw: continue
        arr = raw[key]
        try:
            if key.startswith("int_"):
                out.append({"label":label,"price":float(arr[1]),"change_pct":float(arr[3])})
            else:
                out.append({"label":label,"price":float(arr[1]),"change_pct":float(arr[2])})
        except Exception: pass
    return out

def fetch_a_indices():
    raw = fetch_sina(A_INDEX)
    out = []
    for key, label in A_LABELS.items():
        if key in raw:
            try:
                arr = raw[key]
                out.append({"label":label,"price":float(arr[1]),"change_pct":float(arr[3])})
            except Exception: pass
    return out

def fetch_semi_chain():
    out = []
    raw = fetch_sina(SEMI_US)
    for key, label in SEMI_LABELS.items():
        if key in raw:
            try:
                arr = raw[key]
                out.append({"market":"US","label":label,"price":float(arr[1]),"change_pct":float(arr[2])})
            except Exception: pass
    raw = fetch_sina(SEMI_HK)
    for key, label in SEMI_HK_L.items():
        if key in raw:
            try:
                arr = raw[key]
                out.append({"market":"HK","label":label,"price":float(arr[6]),"change_pct":float(arr[8])})
            except Exception: pass
    return out

def fetch_cls_news(n=12):
    try:
        r = requests.get("https://www.cls.cn/nodeapi/updateTelegraphList",
            params={"app":"CailianpressWeb","rn":n,"os":"web"},
            headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        items = r.json().get("data",{}).get("roll_data",[])
    except Exception:
        return []
    out = []
    for it in items[:n]:
        title = (it.get("title") or "").strip()
        brief = (it.get("brief") or "").replace("\n"," ").strip()[:150]
        if title or brief: out.append({"title":title,"brief":brief})
    return out

def fetch_holdings():
    codes = ",".join(h["code"] for h in HOLDINGS)
    raw = fetch_sina(codes)
    out = []
    for h in HOLDINGS:
        if h["code"] in raw:
            try:
                arr = raw[h["code"]]
                price = float(arr[3])
                pre = float(arr[2])
                out.append({"code":h["code"],"name":h["name"],"weight":h["weight"],
                            "price":price,"change_pct":round((price-pre)/pre*100,2)})
            except Exception:
                out.append({"code":h["code"],"name":h["name"],"weight":h["weight"],"price":None,"change_pct":None})
    return out

def call_llm(prompt):
    try:
        r = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization":f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
            json={"model":"deepseek-chat","max_tokens":1800,"messages":[{"role":"user","content":prompt}]},
            timeout=120)
        data = r.json()
        if data.get("choices"):
            log(f"  AI: deepseek OK")
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  AI 失败: {e}")
    return "（AI 综合失败，请人工查看原始数据）"

def md_to_html(md):
    md = re.sub(r'```json[\s\S]*?```', '', md, flags=re.IGNORECASE)
    md = re.sub(r'\n---\s*$', '', md.strip())
    out = []; in_list = False
    def flush(): nonlocal in_list
    if in_list: out.append("</ul>"); in_list = False
    for line in md.split("\n"):
        s = line.rstrip()
        s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
        if s.startswith("### "): flush(); out.append(f"<h3>{s[4:]}</h3>")
        elif s.startswith("## "): flush(); out.append(f"<h2>{s[3:]}</h2>")
        elif s in ("---","***"): flush(); out.append("<hr>")
        elif s.startswith("- ") or s.startswith("* "):
            if not in_list: out.append("<ul>"); in_list = True
            out.append(f"<li>{s[2:]}</li>")
        elif s == "": flush(); out.append("")
        else: flush(); out.append(f"<p>{s}</p>")
    flush()
    return "\n".join(out)

def push(title, content):
    r = requests.post("http://www.pushplus.plus/send", json={
        "token":PUSHPLUS_TOKEN,"title":title,"template":"html","content":content}, timeout=20)
    return r.status_code == 200 and r.json().get("code") == 200

def main():
    log("=" * 50)
    log("盘前简报 GitHub Actions 版 启动")
    log("拉取美股 + 中概...")
    us = fetch_us_concepts()
    log("拉取 A 股指数...")
    a = fetch_a_indices()
    log("拉取半导体海外联动...")
    semi = fetch_semi_chain()
    log("拉取财联社...")
    news = fetch_cls_news(12)
    log("拉取持仓...")
    holds = fetch_holdings()

    # build prompt
    us_lines = "\n".join(f'- {q["label"]}: {q["price"]:.2f}（{"+" if q["change_pct"]>=0 else ""}{q["change_pct"]:.2f}%）' for q in us)
    a_lines = "\n".join(f'- {q["label"]}: {q["price"]:.2f}（{"+" if q["change_pct"]>=0 else ""}{q["change_pct"]:.2f}% 昨日收盘）' for q in a)
    semi_lines = "\n".join(f'- [{q["market"]}] {q["label"]}: {q["price"]:.2f}（{"+" if q["change_pct"]>=0 else ""}{q["change_pct"]:.2f}%）' for q in semi) if semi else "（无数据）"
    news_lines = "\n".join(f'{i+1}. {(n["title"] or n["brief"])[:120]}' for i,n in enumerate(news))
    holds_lines = "\n".join(f'- {h["name"]}({h["code"]}) **仓位 {h["weight"]}%** 昨收{h["price"]} ({"+" if (h["change_pct"] or 0)>=0 else ""}{h["change_pct"]}%)' if h["price"] else f'- {h["name"]}({h["code"]}) 数据缺失' for h in holds)

    prompt = f"""你是 A 股盘前简报师，精通"养家心法"短线交易框架。现在是开盘前 30 分钟，根据以下数据为用户准备一份盘前简报。

## 隔夜美股 + 中概
{us_lines}

## 半导体产业链海外联动（与持仓相关）
{semi_lines}

## A 股指数（昨日收盘）
{a_lines}

## 财经要闻（最近 {len(news)} 条）
{news_lines}

## 当前持仓
{holds_lines}

请按以下结构输出 Markdown（300-500 字，简洁有力）：

### 一、隔夜市场
2-3 句话总结：美股表现 + 中概表现 + 对今日 A 股的开盘影响判断。

### 二、关键事件
从财经要闻中挑出今日最值得关注的 3-5 条，每条 1 行。

### 三、板块判断
今日哪些方向/板块可能受益、哪些可能受冲击。

### 四、持仓盘前判断
对每只持仓各 1 句，今日开盘需要重点关注什么。

### 五、养家心法提醒
1 句话，提醒今日如何判断情绪、控制仓位。"""

    log("调用 AI...")
    briefing = call_llm(prompt)

    us_table = "".join(f'<tr><td>{q["label"]}</td><td>{q["price"]:.2f}</td><td style="color:{"red" if q["change_pct"]>=0 else "green"}">{"+" if q["change_pct"]>=0 else ""}{q["change_pct"]:.2f}%</td></tr>' for q in us)

    html = f"""<h2>📊 盘前简报 {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>
<details open><summary><b>隔夜美股 + 中概</b></summary>
<table style="border-collapse:collapse"><tr><th>名称</th><th>价格</th><th>涨跌</th></tr>{us_table}</table></details>
<hr>{md_to_html(briefing)}<hr>
<p style="color:#888;font-size:11px">由 GitHub Actions 自动生成 · 不依赖旧 Mac · 仅供参考不构成投资建议</p>"""

    title = f'盘前简报 {datetime.now().strftime("%m/%d %H:%M")}'
    if push(title, html):
        log("推送成功")
    else:
        log("推送失败")
    log("完成")

if __name__ == "__main__":
    main()
