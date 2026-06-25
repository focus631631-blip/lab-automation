# GitHub Actions 迁移 — 操作指南

## 背景
旧 Mac 在家离线时，所有推送（文献日报、盘前简报等）全部瘫痪。
把这些核心推送迁移到 GitHub Actions 后，**不依赖任何家里设备**，只要 GitHub 不倒闭就不会断。

## 你需要做的事（一次性、约 10 分钟）

### 第 1 步：创建 GitHub 仓库
1. 浏览器打开 https://github.com/new
2. Repository name: `lab-automation`（或你喜欢的名字）
3. **选 Private**（私密仓库，代码和配置不对外公开）
4. 不要勾选 "Add a README file"（等会用我们自己的文件）
5. 点 "Create repository"

### 第 2 步：添加 Secrets（API 密钥）
1. 进入仓库 → Settings → Secrets and variables → Actions
2. 点 "New repository secret"，添加两个：

| Name | Value |
|---|---|
| `PUSHPLUS_TOKEN` | （你的 pushplus token，见 `~/.config/secrets.json`）|
| `DEEPSEEK_KEY` | （你的 DeepSeek API key，见 `~/.config/secrets.json`）|

### 第 3 步：推送代码
在本目录下（`github-actions/`）打开终端，执行：

```bash
cd ~/Desktop/claude/多维协作/github-actions
git init -b main
git add -A
git commit -m "GitHub Actions migration: literature + briefing"

# 替换 <你的用户名> 为你的 GitHub 用户名
git remote add origin https://github.com/<你的用户名>/lab-automation.git
git push -u origin main
```

### 第 4 步：验证
1. 进入仓库 → Actions 标签
2. 找到 "文献日报" workflow → 点 "Run workflow" → "Run workflow"（手动触发一次）
3. 等 2-3 分钟，看微信是否收到文献推送

## 文件结构

```
lab-automation/
├── .github/workflows/
│   ├── literature.yml      # 每天 8:00 文献日报
│   └── briefing.yml        # 交易日 8:55 盘前简报
└── github-actions/
    ├── literature_monitor_gh.py
    ├── morning_briefing_gh.py
    └── SETUP.md
```

## 费用
- **完全免费**（GitHub Actions 私人仓库每月 2000 分钟）
- 文献日报：约 90 分钟/月
- 盘前简报：约 66 分钟/月
- 合计：156 分钟/月——只用 8% 的免费额度

## 和旧 Mac 的分工

| 推送 | 跑在 | 依赖旧 Mac |
|---|---|---|
| 📚 文献日报 (8:00) | **GitHub Actions** | ❌ 不再依赖 |
| 📊 盘前简报 (8:55) | **GitHub Actions** | ❌ 不再依赖 |
| 🔔 竞价提醒 (9:27) | 旧 Mac | ✅ |
| 💼 持仓监控 (15:10) | 旧 Mac | ✅ |
| 📊 stock-radar (15:30) | 新 Mac | ❌（但需开机）|
| 🌙 盘后扫描 (18:00) | 旧 Mac | ✅ |
| 🔧 健康检查 | 旧 Mac | ✅ |

**核心 2 条（文献+简报）不依赖任何设备**，其余推送作为补充保留在旧 Mac。

## 以后怎么改关键词/持仓

### 改文献关键词
编辑 `github-actions/literature_monitor_gh.py` 顶部的 `SEARCH_KEYWORDS_JSON` 或直接在 GitHub 网页上编辑 → commit 后自动生效。

### 改持仓
编辑 `github-actions/morning_briefing_gh.py` 顶部的 `HOLDINGS_JSON` 或在 GitHub 网页上编辑 → commit 后自动生效。

也可以用 GitHub Secrets 存 JSON（更灵活，不需要改代码），后续告诉我我帮你配。
