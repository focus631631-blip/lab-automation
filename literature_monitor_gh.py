#!/usr/bin/env python3
"""GitHub Actions 版文献监控 — 每天 8:00 UTC+8 触发
与旧 Mac 版的区别：
- 密钥从环境变量读取（GitHub Secrets）
- 推送历史用 GitHub Actions cache（跨运行持久化）
- 不生成 digest 文件（纯推送）
"""
import os, sys, json, time, xml.etree.ElementTree as ET, requests, re
from datetime import datetime, timedelta

# ========== 从环境变量读取 ==========
PUSHPLUS_TOKEN = os.environ["PUSHPLUS_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
SEARCH_KEYWORDS_JSON = os.environ.get("LIT_KEYWORDS", "")
if SEARCH_KEYWORDS_JSON:
    SEARCH_KEYWORDS = json.loads(SEARCH_KEYWORDS_JSON)
else:
    SEARCH_KEYWORDS = [
        "Neonatal Surgery", "Necrotizing Enterocolitis", "Biliary Atresia",
        "Anorectal Malformations", "Hirschsprung Disease", "Esophageal Atresia",
        "Intestinal Atresia", "Congenital Pulmonary Airway Malformation",
        "Congenital Diaphragmatic Hernia", "Intestinal Malrotation",
        "Hirschsprung Allied Disorders", "Neonatal Ovarian Cyst",
        "Annular Pancreas", "Choledochal Cyst",
    ]
SEARCH_DAYS = int(os.environ.get("SEARCH_DAYS", "7"))
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "100"))

# ========== GitHub Actions cache 持久化 history ==========
CACHE_FILE = "/tmp/pushed_history.json"

def load_history():
    if os.path.exists(CACHE_FILE):
        try:
            return json.loads(open(CACHE_FILE).read())
        except Exception:
            pass
    # Fallback: 尝试从 Actions cache 恢复
    cache_path = os.environ.get("ACTIONS_CACHE_PATH", "")
    if cache_path and os.path.exists(cache_path + "/pushed_history.json"):
        try:
            data = json.loads(open(cache_path + "/pushed_history.json").read())
            open(CACHE_FILE, "w").write(json.dumps(data))
            return data
        except Exception:
            pass
    return []

def save_history(history):
    open(CACHE_FILE, "w").write(json.dumps(history, ensure_ascii=False))
    # 写入 cache 路径（Actions 用）
    cache_path = os.environ.get("ACTIONS_CACHE_OUT", "")
    if cache_path:
        try:
            os.makedirs(cache_path, exist_ok=True)
            open(cache_path + "/pushed_history.json", "w").write(json.dumps(history, ensure_ascii=False))
        except Exception:
            pass

# ========== 日志 ==========
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ========== PubMed ==========
def ncbi_get(url, params, retries=3):
    """NCBI eutils 请求带重试。NCBI 不带 API key 限流 3 次/秒，偶发返回限流页/空响应，
    重试可消化大部分抽风。全部失败返回 None，由调用方决定跳过。"""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200 and r.text.strip():
                return r
        except Exception:
            pass
        if attempt < retries:
            time.sleep(2 * attempt)  # 2s,4s 退避
    return None

def search_pubmed(keyword, days=7, max_results=5):
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    date_to = datetime.now().strftime("%Y/%m/%d")
    r = ncbi_get(base_url, {
        "db": "pubmed", "term": keyword, "retmax": max_results,
        "sort": "date", "datetype": "pdat",
        "mindate": date_from, "maxdate": date_to, "retmode": "json",
    })
    if r is None:
        log(f"  ⚠️ esearch 失败(NCBI 无响应)，跳过关键词: {keyword}")
        return []
    try:
        return r.json().get("esearchresult", {}).get("idlist", [])
    except Exception:
        log(f"  ⚠️ esearch 返回非 JSON，跳过关键词: {keyword}")
        return []

def fetch_article_details(pmids):
    if not pmids:
        return []
    r = ncbi_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", {
        "db": "pubmed", "id": ",".join(pmids), "retmode": "xml",
    })
    if r is None:
        log("  ⚠️ efetch 失败(NCBI 无响应)，跳过这批 PMID")
        return []
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        log("  ⚠️ efetch 返回非 XML(多为限流)，跳过这批 PMID")
        return []
    articles = []
    for article in root.findall(".//PubmedArticle"):
        try:
            title_elem = article.find(".//ArticleTitle")
            title = title_elem.text if title_elem is not None else "No Title"
            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join(p.text for p in abstract_parts if p.text) if abstract_parts else ""
            doi = ""
            for id_elem in article.findall(".//ArticleId"):
                if id_elem.get("IdType") == "doi":
                    doi = id_elem.text
                    break
            pmid_elem = article.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else ""
            if title and abstract:
                articles.append({"pmid": pmid, "title": title, "abstract": abstract, "doi": doi or "未提供"})
        except Exception:
            continue
    return articles

# ========== medRxiv ==========
def fetch_medrxiv_recent(days=7):
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")
    all_papers = []
    cursor = 0
    while True:
        try:
            r = requests.get(f"https://api.biorxiv.org/details/medrxiv/{date_from}/{date_to}/{cursor}", timeout=30)
            data = r.json()
        except Exception as e:
            log(f"  medRxiv 拉取失败 cursor={cursor}: {e}")
            break
        batch = data.get("collection", [])
        if not batch:
            break
        all_papers.extend(batch)
        meta = data.get("messages", [{}])[0]
        total = int(meta.get("total", 0))
        if cursor + len(batch) >= total or cursor > 5000:
            break
        cursor += len(batch)
        time.sleep(0.5)
    log(f"  medRxiv 共拉取 {len(all_papers)} 篇预印本")
    return all_papers

def search_medrxiv(papers, keyword, max_results=5):
    kw = keyword.lower()
    matched = []
    for it in papers:
        if kw in (it.get("title") or "").lower() or kw in (it.get("abstract") or "").lower():
            doi = it.get("doi", "")
            matched.append({
                "pmid": f"medrxiv_{doi}", "title": it.get("title", ""),
                "abstract": it.get("abstract", ""), "doi": doi or "未提供",
                "source": "medRxiv (preprint)", "date": it.get("date", ""),
            })
            if len(matched) >= max_results:
                break
    return matched

# ========== AI ==========
def _deepseek_call(prompt, model):
    """单次 DeepSeek 调用，成功返回文本，失败返回 None。"""
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.status_code != 200:
            log(f"  DeepSeek[{model}] HTTP {r.status_code}: {r.text[:120]}")
            return None
        data = r.json()
        if data.get("choices") and data["choices"][0].get("message"):
            content = data["choices"][0]["message"].get("content", "").strip()
            if content:
                return content
        log(f"  DeepSeek[{model}] 返回空内容")
    except Exception as e:
        log(f"  DeepSeek[{model}] 异常: {e}")
    return None


def _fallback_abstract(abstract):
    """AI 全部失败时的兜底：直接给英文摘要截断，保证有内容可读而不是'生成失败'。"""
    abs_txt = (abstract or "").strip()
    if not abs_txt:
        return "- ⚠️ AI 总结暂时不可用，且原文无摘要，请点标题看 PubMed 原文。"
    if len(abs_txt) > 900:
        abs_txt = abs_txt[:900].rsplit(" ", 1)[0] + " …"
    return (
        "- ⚠️ AI 总结通道暂时不可用，以下为英文原文摘要（截断），请对照原文：\n"
        + abs_txt
    )


def generate_summary(title, abstract):
    prompt = f"""你是一位新生儿外科领域的医学专家。请阅读以下英文文献的标题和摘要，用中文生成结构化总结。

【标题】: {title}
【摘要】: {abstract}

请严格按以下格式输出（不要加任何额外内容）：
- 研究目的：（一句话概括）
- 核心发现/数据：（2-3句话，包含关键数据）
- 临床启示：（1-2句话，对临床实践的意义）"""
    # 通道1：deepseek-chat，失败重试一次；通道2：deepseek-reasoner 兜底；最后给英文摘要
    attempts = [("deepseek-chat", 1), ("deepseek-chat", 2), ("deepseek-reasoner", 1)]
    for model, attempt in attempts:
        out = _deepseek_call(prompt, model)
        if out:
            return out
        log(f"  总结失败，切下一通道（刚才：{model} 第{attempt}次）")
        time.sleep(2)
    log("  所有 AI 通道均失败，回退到英文摘要")
    return _fallback_abstract(abstract)

# ========== 推送 ==========
# PushPlus 单条 content 上限 2 万字，留余量防止被拒
MAX_PUSH_CHARS = 19000

def push(title, content, retries=3):
    """返回 (ok, code, msg)；对网络抖动/PushPlus 偶发错误自动重试。
    注意：内容超限(code 999)等确定性错误重试无意义，但代价极小，统一重试更简单。"""
    last_code, last_msg = None, None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post("http://www.pushplus.plus/send", json={
                "token": PUSHPLUS_TOKEN, "title": title, "template": "html", "content": content,
            }, timeout=20)
            data = r.json()
            last_code = data.get("code")
            last_msg = data.get("msg") or data.get("data")
            if r.status_code == 200 and last_code == 200:
                return True, last_code, last_msg
        except Exception as e:
            last_code, last_msg = -1, str(e)
        if attempt < retries:
            log(f"  推送第 {attempt} 次未成功(code={last_code})，{5}s 后重试")
            time.sleep(5)
    return False, last_code, last_msg

def push_heartbeat(stats):
    title = f"📡 文献监控心跳 {datetime.now().strftime('%m/%d')}"
    content = f"""<h3>📚 今日 0 篇新文献（GitHub Actions 版）</h3>
<ul><li>PubMed: 14 个关键词，命中 <b>{stats.get('pubmed_total', 0)}</b> 篇</li>
<li>medRxiv: 拉取 <b>{stats.get('medrxiv_total', 0)}</b> 篇预印本</li></ul>
<p style="color:#888;font-size:11px">由 GitHub Actions 自动运行 · 不依赖旧 Mac</p>"""
    return push(title, content)

# ========== 主流程 ==========
def main():
    log("=" * 50)
    log("文献监控 GitHub Actions 版 启动")
    history = load_history()
    new_articles = []
    stats = {"pubmed_total": 0, "medrxiv_total": 0}

    for kw in SEARCH_KEYWORDS:
        log(f"检索: {kw}")
        pmids = search_pubmed(kw, days=SEARCH_DAYS, max_results=5)
        stats["pubmed_total"] += len(pmids)
        log(f"  找到 {len(pmids)} 篇")
        if pmids:
            new_pmids = [p for p in pmids if p not in history]
            if new_pmids:
                articles = fetch_article_details(new_pmids)
                new_articles.extend(articles)
                log(f"  新文献 {len(articles)} 篇")
        time.sleep(0.5)

    log("扫描 medRxiv...")
    try:
        pool = fetch_medrxiv_recent(days=SEARCH_DAYS)
        stats["medrxiv_total"] = len(pool)
        for kw in SEARCH_KEYWORDS:
            matches = search_medrxiv(pool, kw, max_results=5)
            new_matches = [m for m in matches if m["pmid"] not in history]
            if new_matches:
                log(f"  [medRxiv] {kw}: 新 {len(new_matches)}")
                new_articles.extend(new_matches)
    except Exception as e:
        log(f"  medRxiv 阶段失败: {e}")

    # 去重
    seen = set()
    unique = []
    for a in new_articles:
        if a["pmid"] not in seen:
            seen.add(a["pmid"])
            unique.append(a)
    unique = unique[:MAX_ARTICLES]
    log(f"共发现 {len(unique)} 篇新文献")

    if not unique:
        # 备用运行(中午/晚上)无新文献时静默，避免每天多推心跳打扰；
        # 只有主运行(早上8:00)才发“今日0篇”心跳作为存活信号。
        if os.environ.get("RUN_ROLE", "primary") == "backup":
            log("备用运行且无新文献，静默跳过")
        else:
            log("无新文献，推送心跳")
            push_heartbeat(stats)
        log("完成")
        return

    # 逐篇 AI 总结
    parts = []
    for i, a in enumerate(unique):
        log(f"处理 [{i+1}/{len(unique)}]: {a['title'][:50]}...")
        summary = generate_summary(a["title"], a["abstract"])
        a["_summary"] = summary
        time.sleep(1)
        doi_link = f'https://doi.org/{a["doi"]}' if a["doi"] != "未提供" else "未提供"
        source = a.get("source", "PubMed")
        parts.append(f"""<div style="border:1px solid #ddd;padding:12px;margin:10px 0;border-radius:8px">
<h3>★ {source} ★</h3>
<p><b>【原文题目】</b>: {a['title']}</p>
<p><b>【DOI 号】</b>: <a href="{doi_link}">{a['doi']}</a></p>
<p><b>【核心中文总结】</b>:</p>
<p>{summary.replace(chr(10), '<br>')}</p></div>""")
        history.append(a["pmid"])

    today = datetime.now().strftime("%Y-%m-%d")
    header = f"<h2>📚 新生儿外科文献日报 ({today})</h2>"
    footer = '<p style="color:#888;font-size:11px">由 GitHub Actions 自动生成 · 不依赖旧 Mac</p>'
    wrap_overhead = len(header) + len(footer) + 120  # 头尾 + 提示行余量

    # 按字符预算分批，保证每条 content 不超过 PushPlus 上限
    batches, cur, cur_len = [], [], 0
    for p in parts:
        if cur and cur_len + len(p) + wrap_overhead > MAX_PUSH_CHARS:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p)
    if cur:
        batches.append(cur)

    n = len(batches)
    all_ok = True
    for bi, batch in enumerate(batches, 1):
        seq = f"（{bi}/{n}）" if n > 1 else ""
        title = f"文献日报{seq}: {len(batch)}篇新生儿外科文献 ({datetime.now().strftime('%m/%d')})"
        body = f"{header}<p>本条含 <b>{len(batch)}</b> 篇（共 {len(parts)} 篇新文献）：</p>{''.join(batch)}{footer}"
        ok, code, msg = push(title, body)
        if ok:
            log(f"推送成功 {seq} {len(body)} 字")
        else:
            all_ok = False
            log(f"推送失败 {seq}: code={code} msg={msg}（{len(body)} 字）")

    if all_ok:
        save_history(history)
        log(f"完成：{n} 条推送全部成功")
    else:
        log("有推送失败，未保存历史，下次运行会重试")
        # 单独推一条极短的报警，避免“一片安静”让用户以为只是今天没新文献
        alert_ok, _, _ = push(
            f"⚠️ 文献日报推送失败 {datetime.now().strftime('%m/%d')}",
            "<p>今日文献抓取正常，但推送失败（详情见 GitHub Actions 日志）。历史未保存，下次会自动重试。</p>",
        )
        log(f"报警推送{'成功' if alert_ok else '也失败了'}")
        sys.exit(1)  # 让 GitHub Actions 显示失败（红色），便于及时发现

if __name__ == "__main__":
    main()
