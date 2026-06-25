#!/usr/bin/env python3
"""GitHub Actions 版竞价提醒 — 交易日 9:27 触发
简化版：直接检测持仓竞价开盘价 + 大盘指数，AI 给操作建议"""
import os, sys, json, re, requests, subprocess
from datetime import datetime

PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
HOLDINGS_JSON = os.environ.get("HOLDINGS", '[{"code":"sh600089","name":"特变电工","weight":70,"cost":26.5},{"code":"sz002028","name":"思源电气","weight":30,"cost":200}]')
HOLDINGS = json.loads(HOLDINGS_JSON)

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_sina(symbols):
    r = subprocess.run(["curl","-s","-m","15",f"http://hq.sinajs.cn/list={symbols}",
        "-H","Referer: https://finance.sina.com.cn/"], capture_output=True, timeout=20)
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

def call_llm(prompt):
    try:
        r = requests.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization":f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
            json={"model":"deepseek-chat","max_tokens":600,"messages":[{"role":"user","content":prompt}]},
            timeout=60)
        if r.json().get("choices"):
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  AI 失败: {e}")
    return "（AI 不可用，请根据下方数据自行判断）"

def push(title, content):
    r = requests.post("http://www.pushplus.plus/send", json={
        "token":PUSHPLUS_TOKEN,"title":title,"template":"html","content":content}, timeout=20)
    return r.status_code == 200 and r.json().get("code") == 200

def main():
    log("=" * 50)
    log("竞价提醒 GitHub Actions 版 启动")

    # 拉大盘 + 持仓
    codes = [h["code"] for h in HOLDINGS]
    indices = ["s_sh000001","s_sz399001","s_sz399006"]
    raw = fetch_sina(",".join(codes + indices))

    # 解析持仓竞价
    holds = []
    for h in HOLDINGS:
        if h["code"] not in raw: continue
        arr = raw[h["code"]]
        price = float(arr[3])
        pre = float(arr[2])
        opn = float(arr[1])
        pct = round((price - pre) / pre * 100, 2)
        gap_pct = round((opn - pre) / pre * 100, 2)  # 竞价涨幅
        cost_pct = round((price - h["cost"]) / h["cost"] * 100, 2) if "cost" in h else 0
        holds.append({**h, "price":price, "open":opn, "pct":pct, "gap_pct":gap_pct, "cost_pct":cost_pct})

    # 大盘
    idx_map = {}
    for key, label in [("s_sh000001","上证"),("s_sz399001","深证"),("s_sz399006","创业板")]:
        if key in raw:
            arr = raw[key]
            idx_map[label] = f'{float(arr[1]):.0f} ({float(arr[3]):+.2f}%)'

    # 构建 prompt
    holds_lines = "\n".join(
        f'- {h["name"]}({h["code"]}): 竞价开{h["open"]:.2f}（{h["gap_pct"]:+.2f}%），'
        f'现价{h["price"]:.2f}（日涨{h["pct"]:+.2f}%），成本{h["cost"]}（浮{"盈" if h["cost_pct"]>0 else "亏"}{h["cost_pct"]:+.2f}%）'
        for h in holds
    )
    idx_lines = "\n".join(f'- {k}: {v}' for k,v in idx_map.items())

    prompt = f"""你是超短线交易专家。以下是集合竞价阶段数据，请用2-3句话给出操作建议。

【大盘】
{idx_lines}

【持仓竞价】
{holds_lines}

要求：简洁直接，像老手盘前提醒。判断今日开盘情绪，对每只持仓给出"追/等/放弃"建议。"""

    log("调用 AI...")
    comment = call_llm(prompt)

    # 渲染
    cards = "".join(
        f'<tr><td><b>{h["name"]}</b></td>'
        f'<td>{h["price"]:.2f}</td>'
        f'<td style="color:{"red" if h["gap_pct"]>0 else "green"}">{h["gap_pct"]:+.2f}%</td>'
        f'<td>{h["cost"]}</td>'
        f'<td style="color:{"red" if h["cost_pct"]>0 else "green"}">{h["cost_pct"]:+.2f}%</td></tr>'
        for h in holds
    )

    now = datetime.now().strftime("%H:%M")
    html = f"""<h2>🔔 竞价提醒（{now}）</h2>
<table style="border-collapse:collapse;width:100%">
<tr style="background:#f3f4f6"><th>标的</th><th>现价</th><th>竞价涨幅</th><th>成本</th><th>浮盈亏</th></tr>
{cards}</table>
<h3>操作建议</h3>
<p>{comment.replace(chr(10), '<br>')}</p>
<p style="color:#888;font-size:11px">GitHub Actions 自动生成 · 仅供参考</p>"""

    title = f'竞价提醒 {datetime.now().strftime("%m/%d")} {now}'
    if push(title, html):
        log("推送成功")
    else:
        log("推送失败")
    log("完成")

if __name__ == "__main__":
    main()
