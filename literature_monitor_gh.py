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

# easyScholar 期刊分区/影响因子（可选：未配置 key 时自动跳过，不影响推送）
EASYSCHOLAR_KEY = os.environ.get("EASYSCHOLAR_KEY", "").strip()
RANK_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_rank_cache.json")
RANK_TTL_DAYS = 180  # 分区/IF 每年更新一次，半年缓存足够新，且大幅省接口额度

# ========== GitHub Actions cache 持久化 history ==========
# 历史存回仓库根目录（随 checkout 带下来），运行后由 workflow 提交回仓库持久化。
# 只保留最近 MAX_HISTORY 条，防止文件无限膨胀。
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pushed_history.json")
MAX_HISTORY = 2000

def load_history():
    if os.path.exists(CACHE_FILE):
        try:
            return json.loads(open(CACHE_FILE).read())
        except Exception:
            pass
    return []

def save_history(history):
    trimmed = history[-MAX_HISTORY:]
    open(CACHE_FILE, "w").write(json.dumps(trimmed, ensure_ascii=False))

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
            # 期刊：全名(用于查分区，匹配更准) + ISO 缩写(用于显示)
            jt = article.find(".//Journal/Title")
            ja = article.find(".//Journal/ISOAbbreviation")
            journal = jt.text if jt is not None and jt.text else ""
            journal_abbr = ja.text if ja is not None and ja.text else ""
            if title and abstract:
                articles.append({
                    "pmid": pmid, "title": title, "abstract": abstract, "doi": doi or "未提供",
                    "journal": journal, "journal_abbr": journal_abbr,
                })
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

# ========== easyScholar 期刊分区 / 影响因子 ==========
_rank_cache = None

def _load_rank_cache():
    global _rank_cache
    if _rank_cache is None:
        try:
            _rank_cache = json.loads(open(RANK_CACHE_FILE).read())
        except Exception:
            _rank_cache = {}
    return _rank_cache

def save_rank_cache():
    if _rank_cache is not None:
        try:
            open(RANK_CACHE_FILE, "w", encoding="utf-8").write(
                json.dumps(_rank_cache, ensure_ascii=False, indent=1))
        except Exception as e:
            log(f"  分区缓存写入失败: {e}")

def _fmt_rank(all_data):
    """把 easyScholar officialRank.all 整理成简短中文徽章；缺字段就跳过对应段。
    形如：中科院医学2区(儿科1区/外科2区) · IF 2.3 · JCR Q2"""
    if not all_data:
        return ""
    parts = []
    cas = (all_data.get("sciUp") or "").strip()                       # 中科院升级版大类
    cas_small = (all_data.get("sciUpSmall") or "").strip().rstrip("。")  # 小类(儿科/外科)
    if cas:
        parts.append(f"中科院{cas}" + (f"({cas_small})" if cas_small else ""))
    iff = (all_data.get("sciif") or "").strip()
    if iff:
        parts.append(f"IF {iff}")
    jcr = (all_data.get("sci") or "").strip()
    if jcr:
        parts.append(f"JCR {jcr}")
    return " · ".join(parts)

def lookup_journal_rank(journal):
    """查期刊分区/IF，返回简短中文徽章（未配置 key / 查不到 / 出错都返回空串）。
    持久缓存 + 限流(40006)退避重试；任何异常都安静降级，绝不影响推送主流程。"""
    journal = (journal or "").strip()
    if not journal or not EASYSCHOLAR_KEY:
        return ""
    cache = _load_rank_cache()
    ckey = journal.lower()
    now = datetime.now()
    hit = cache.get(ckey)
    if hit:
        try:
            ts = datetime.strptime(hit.get("_ts", "2000-01-01"), "%Y-%m-%d")
            if (now - ts).days < RANK_TTL_DAYS:
                return hit.get("badge", "")
        except Exception:
            pass
    for attempt in range(1, 4):
        try:
            r = requests.get(
                "https://www.easyscholar.cc/open/getPublicationRank",
                params={"secretKey": EASYSCHOLAR_KEY, "publicationName": journal}, timeout=20)
            data = r.json()
        except Exception as e:
            log(f"  easyScholar 请求异常({journal}): {e}")
            return ""  # 网络异常不缓存，下次运行重试
        code = data.get("code")
        if code == 200:
            all_data = ((data.get("data") or {}).get("officialRank") or {}).get("all") or {}
            badge = _fmt_rank(all_data)
            cache[ckey] = {"badge": badge, "_ts": now.strftime("%Y-%m-%d")}  # 空徽章也缓存，避免反复查找不到的刊
            return badge
        if code == 40006:  # 请求频繁
            log(f"  easyScholar 限流，{2*attempt}s 后重试({journal})")
            time.sleep(2 * attempt)
            continue
        # 其它错误码(额度用尽等)：不缓存，下次再试
        log(f"  easyScholar code={code} msg={data.get('msg')} ({journal})")
        return ""
    return ""

def journal_display(a):
    """展示用期刊名：优先 ISO 缩写，其次全名，最后回退来源(medRxiv 预印本等)。"""
    return (a.get("journal_abbr") or a.get("journal") or a.get("source") or "").strip()

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

# ========== 归档 ==========
def archive_articles(articles):
    """把当天推送的文献追加归档到 archive/YYYY-MM-DD.md，随历史一起提交回仓库。
    含标题/中文总结/原文摘要/DOI/PubMed 链接，需要全文时点链接去原站获取。"""
    day = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%H:%M")
    arc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
    os.makedirs(arc_dir, exist_ok=True)
    path = os.path.join(arc_dir, f"{day}.md")
    lines = []
    if not os.path.exists(path):
        lines.append(f"# 新生儿外科文献归档 · {day}\n")
    for a in articles:
        pmid = str(a.get("pmid", ""))
        doi = a.get("doi", "未提供")
        links = []
        if doi and doi != "未提供":
            links.append(f"[DOI 全文](https://doi.org/{doi})")
        if pmid.isdigit():
            links.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)")
        link_line = " · ".join(links) if links else "无直达链接"
        lines.append(f"\n## {a.get('title', '(无标题)')}\n")
        lines.append(f"- 来源: {a.get('source', 'PubMed')} · 推送 {ts}")
        jname = journal_display(a)
        if jname:
            badge = a.get("_rank_badge", "")
            lines.append(f"- 期刊: {jname}" + (f" · {badge}" if badge else ""))
        lines.append(f"- DOI: {doi}")
        lines.append(f"- 获取全文: {link_line}\n")
        summary = (a.get("_summary") or "").strip()
        if summary:
            lines.append(f"**中文总结**：{summary}\n")
        abstract = (a.get("abstract") or "").strip()
        if abstract:
            lines.append(f"**原文摘要**：{abstract}\n")
        lines.append("---")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # 结构化数据 + 生成网页（供 GitHub Pages 公开访问）
    _update_data_and_site(articles, day)
    log(f"已归档 {len(articles)} 篇到 archive/{day}.md，并更新网页")

# ========== 结构化数据 + 网页（GitHub Pages） ==========
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(REPO_ROOT, "archive", "data.json")
INDEX_FILE = os.path.join(REPO_ROOT, "index.html")
MAX_SITE_ITEMS = 1000

def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _update_data_and_site(articles, day):
    data = []
    if os.path.exists(DATA_FILE):
        try:
            data = json.loads(open(DATA_FILE).read())
        except Exception:
            data = []
    have = {d.get("pmid") for d in data}
    for a in articles:
        pmid = str(a.get("pmid", ""))
        if pmid in have:
            continue
        data.append({
            "date": day, "pmid": pmid, "title": a.get("title", ""),
            "doi": a.get("doi", "未提供"), "source": a.get("source", "PubMed"),
            "journal": journal_display(a), "rank": a.get("_rank_badge", ""),
            "summary": (a.get("_summary") or "").strip(),
            "abstract": (a.get("abstract") or "").strip(),
        })
    data = data[-MAX_SITE_ITEMS:]
    open(DATA_FILE, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
    render_index_html(data)

def render_index_html(data):
    by_date = {}
    for d in data:
        by_date.setdefault(d["date"], []).append(d)
    blocks = []
    for day in sorted(by_date, reverse=True):
        items = by_date[day]
        blocks.append(f'<h2 class="day">{day}<span class="cnt">{len(items)} 篇</span></h2>')
        for a in items:
            links = []
            doi = a.get("doi", "")
            if doi and doi != "未提供":
                links.append(f'<a href="https://doi.org/{_esc(doi)}" target="_blank" rel="noopener">DOI 全文</a>')
            pmid = str(a.get("pmid", ""))
            if pmid.isdigit():
                links.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{pmid}/" target="_blank" rel="noopener">PubMed</a>')
            link_html = " · ".join(links) if links else "<span class=nolink>无直达链接</span>"
            summ = _esc(a.get("summary", "")).replace("\n", "<br>")
            abst = _esc(a.get("abstract", ""))
            details = f'<details><summary>原文摘要</summary><p>{abst}</p></details>' if abst else ""
            jn = _esc(a.get("journal", ""))
            rk = _esc(a.get("rank", ""))
            jr = ""
            if jn:
                jr = f'<div class="jrnl">{jn}' + (f' · <b>{rk}</b>' if rk else "") + '</div>'
            blocks.append(
                f'<article><h3>{_esc(a.get("title", ""))}</h3>'
                f'{jr}'
                f'<div class="meta"><span class="src">{_esc(a.get("source", "PubMed"))}</span> · {link_html}</div>'
                f'<div class="sum">{summ}</div>{details}</article>'
            )
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>新生儿外科文献归档</title><style>"
        ":root{color-scheme:light dark}"
        "*{box-sizing:border-box}"
        "body{margin:0;font-family:-apple-system,'PingFang SC',Roboto,sans-serif;line-height:1.6;"
        "background:#f6f7f9;color:#1a1a1a}"
        "header{background:#0b6b5b;color:#fff;padding:20px 16px}"
        "header h1{margin:0;font-size:20px}header p{margin:4px 0 0;opacity:.85;font-size:13px}"
        "main{max-width:820px;margin:0 auto;padding:16px}"
        ".day{font-size:15px;color:#0b6b5b;border-bottom:2px solid #0b6b5b33;padding-bottom:4px;margin:24px 0 12px;display:flex;justify-content:space-between}"
        ".cnt{font-weight:400;color:#888;font-size:13px}"
        "article{background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:14px 16px;margin:10px 0}"
        "article h3{margin:0 0 8px;font-size:16px}"
        ".meta{font-size:13px;color:#666;margin-bottom:8px}"
        ".src{background:#0b6b5b14;color:#0b6b5b;padding:1px 8px;border-radius:20px;font-size:12px}"
        ".jrnl{font-size:13px;color:#0b6b5b;font-weight:600;margin-bottom:6px}"
        ".meta a{color:#0b6b5b;text-decoration:none;font-weight:600}"
        ".nolink{color:#aaa}"
        ".sum{font-size:14px;white-space:normal}"
        "details{margin-top:8px}summary{cursor:pointer;color:#0b6b5b;font-size:13px}"
        "details p{font-size:13px;color:#444;background:#fafafa;padding:10px;border-radius:6px}"
        "footer{text-align:center;color:#999;font-size:12px;padding:24px}"
        "@media(prefers-color-scheme:dark){body{background:#16181c;color:#e6e6e6}article{background:#1e2126;border-color:#2c2f36}details p{background:#22252b;color:#bbb}}"
        "</style></head><body>"
        "<header><h1>📚 新生儿外科文献归档</h1>"
        f"<p>共 {len(data)} 篇 · 每天 8:00 自动更新 · 最后更新 {updated}</p></header>"
        f"<main>{''.join(blocks) or '<p>暂无归档</p>'}</main>"
        "<footer>由 GitHub Actions 自动生成 · 需要全文请点 DOI / PubMed 链接</footer>"
        "</body></html>"
    )
    open(INDEX_FILE, "w", encoding="utf-8").write(html)

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
        # 期刊分区/IF（缓存优先；medRxiv 无期刊会自动跳过查询）
        badge = lookup_journal_rank(a.get("journal") or a.get("journal_abbr"))
        a["_rank_badge"] = badge
        jname = journal_display(a)
        journal_html = ""
        if jname:
            badge_html = f' · <span style="color:#0b6b5b;font-weight:600">{badge}</span>' if badge else ""
            journal_html = f"<p><b>【期刊】</b>: {jname}{badge_html}</p>\n"
        time.sleep(1)
        doi_link = f'https://doi.org/{a["doi"]}' if a["doi"] != "未提供" else "未提供"
        source = a.get("source", "PubMed")
        parts.append(f"""<div style="border:1px solid #ddd;padding:12px;margin:10px 0;border-radius:8px">
<h3>★ {source} ★</h3>
<p><b>【原文题目】</b>: {a['title']}</p>
{journal_html}<p><b>【DOI 号】</b>: <a href="{doi_link}">{a['doi']}</a></p>
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
        archive_articles(unique)   # 推送成功才归档，保证"归档=已送达"
        save_history(history)
        save_rank_cache()          # 持久化期刊分区缓存，下次同名刊直接命中省额度
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
