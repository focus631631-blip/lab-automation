#!/usr/bin/env python3
"""GitHub Actions 版持仓监控 — 交易日 15:10 触发"""
import os, sys, json, requests, subprocess
from datetime import datetime

PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
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

def push(title, content):
    r = requests.post("http://www.pushplus.plus/send", json={
        "token":PUSHPLUS_TOKEN,"title":title,"template":"html","content":content}, timeout=20)
    return r.status_code == 200 and r.json().get("code") == 200

def main():
    log("=" * 50)
    log("持仓监控 GitHub Actions 版 启动")

    codes = [h["code"] for h in HOLDINGS]
    raw = fetch_sina(",".join(codes))

    rows = ""
    total_pnl = 0
    for h in HOLDINGS:
        if h["code"] not in raw: continue
        arr = raw[h["code"]]
        price = float(arr[3])
        pre = float(arr[2])
        high = float(arr[4])
        low = float(arr[5])
        vol = float(arr[8]) if arr[8] else 0
        amt = float(arr[9]) / 1e8 if arr[9] else 0
        pct = round((price - pre) / pre * 100, 2)
        cost_pnl = round((price - h["cost"]) / h["cost"] * 100, 2) if "cost" in h else 0
        total_pnl += cost_pnl * h["weight"] / 100
        color = "#e53935" if pct > 0 else "#43a047" if pct < 0 else "#666"
        rows += (
            f'<tr><td><b>{h["name"]}</b><br><span style="font-size:11px;color:#999">{h["code"]} {h["weight"]}%</span></td>'
            f'<td style="text-align:right;font-size:16px;font-weight:bold">{price:.2f}</td>'
            f'<td style="text-align:right;color:{color}">{pct:+.2f}%</td>'
            f'<td style="text-align:right">{cost_pnl:+.2f}%</td>'
            f'<td style="text-align:right;font-size:11px">{high:.2f} / {low:.2f}<br>振幅{(high-low)/pre*100:.1f}%</td>'
            f'<td style="text-align:right;font-size:11px;color:#999">{amt:.1f}亿</td></tr>'
        )

    total_color = "#e53935" if total_pnl > 0 else "#43a047"
    now = datetime.now().strftime("%H:%M")
    html = f"""<h2>💼 收盘持仓 {datetime.now().strftime('%m/%d')} {now}</h2>
<table style="border-collapse:collapse;width:100%;font-size:13px">
<tr style="background:#f3f4f6"><th>标的</th><th>现价</th><th>日涨跌</th><th>成本盈亏</th><th>日内高/低</th><th>成交</th></tr>
{rows}</table>
<p style="font-size:15px;margin-top:8px">合计账户浮赢亏: <span style="color:{total_color};font-weight:bold">{total_pnl:+.2f}%</span></p>
<p style="color:#888;font-size:11px">GitHub Actions 自动生成 · 仅供参考</p>"""

    title = f'持仓日报 {datetime.now().strftime("%m/%d")} {now}'
    if push(title, html):
        log("推送成功")
    else:
        log("推送失败")
    log("完成")

if __name__ == "__main__":
    main()
