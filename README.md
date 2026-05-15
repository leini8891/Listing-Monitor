# Listing Monitor

一个轻量、文件驱动的 perp listing intelligence 项目。

现在的结构已经按 pipeline 分层：
- `src/ingestion/`：交易所抓取与 listing 检测
- `src/transform/`：清洗、CoinGecko enrich、RWA 标注、归档、SQLite query layer 构建
- `src/quality/`：数据质量审计
- `src/delivery/`：Lark 推送层
- `src/app/`：Streamlit dashboard
- `config/`：显式配置，如 CoinGecko override map
- `data/`：raw / cache / processed / marts / audits / history / db

## Directory Layout

```text
listing-monitor/
  README.md
  .env
  .env.example
  .gitignore

  config/
    coingecko_overrides.py
    rwa_allowlist.csv

  docs/
    architecture/
      Listing Monitor架构walkthrough.md
      listing_monitor_review_prompt.md

  src/
    ingestion/
      hl_listing_monitor.py
      fetch_venue_ticker_metrics.py
    transform/
      clean_watchboard.py
      enrich_watchboard_coingecko.py
      label_rwa_tokens.py
      archive_daily_snapshot.py
      build_history_store.py
    quality/
      audit_watchboard_quality.py
    delivery/
      lark_listing_watchboard.py
    app/
      streamlit_app.py
      watchboard_query.py
    common/
      paths.py

  data/
    raw/
    cache/
    processed/
    marts/
    audits/
    history/
    db/
```

## Architecture Notes

Project architecture and review context are archived in `docs/architecture/`:
- `Listing Monitor架构walkthrough.md`：comprehensive architecture walkthrough and Claude review output
- `listing_monitor_review_prompt.md`：the review prompt used to generate the architecture assessment

These files are documentation only; they do not affect the runtime pipeline.

## Environment

安装依赖：

```bash
pip install -r requirements.txt
```

创建 `.env`：

```bash
cp .env.example .env
```

填入：

```env
LARK_WEBHOOK_URL=https://open.larksuite.com/open-apis/bot/v2/hook/your-webhook
WATCHBOARD_DASHBOARD_URL=http://localhost:8511/?page=overview
WATCHBOARD_HISTORY_DIFF_URL=http://localhost:8511/?page=history
```

说明：
- 所有脚本都通过 `src/common/paths.py` 按项目根目录解析路径，不依赖当前工作目录
- `lark_listing_watchboard.py` 仍然支持 `--webhook` 显式覆盖 `.env`
- Streamlit Community Cloud 部署只需要 `requirements.txt`，不需要 `.env`

## Makefile

常用入口已经收进 `Makefile`：

```bash
make listings
make clean
make market
make rwa
make tickers
make metrics
make audit
make archive
make db
make lark
make ui
make pipeline
make daily
make all
```

含义：
- `make listings`：一键刷新 listing state 和 `data/raw/listing_watchboard.csv`，不推 Lark
- `make rwa`：生成 `data/processed/token_rwa_labels.csv`
- `make pipeline`：`clean + market + rwa + tickers + metrics + audit + archive + db`
- `make daily`：`pipeline + lark`
- `make all`：`listings + pipeline`

推荐日常用法：

```bash
make all
make daily
make ui
```

## Core Files

主要数据层：
- `data/raw/known_listings.json`
- `data/raw/listing_watchboard.csv`
- `data/cache/coingecko_coin_details_cache.json`
- `data/processed/listing_watchboard_clean.csv`
- `data/processed/token_market_metrics.csv`
- `data/processed/token_rwa_labels.csv`
- `data/processed/venue_ticker_metrics.csv`
- `data/processed/listing_watchboard_token_metrics.csv`
- `data/marts/top_volume_tokens.csv`
- `data/marts/top_gainers_tokens.csv`
- `data/marts/top_losers_tokens.csv`
- `data/marts/hot_new_tokens.csv`
- `data/audits/listing_coverage_audit.csv`
- `data/audits/token_market_match_audit.csv`
- `data/audits/token_market_metrics_audit.csv`
- `data/db/listing_watchboard_history.sqlite`

语义约定：
- `token_market_metrics.csv` = CoinGecko token-level aggregated market data
- `token_rwa_labels.csv` = token-level RWA classification output keyed primarily by `coingecko_id`
- `venue_ticker_metrics.csv` = exchange-specific perp/swap/futures metrics
- 不要把 CoinGecko `volume_24h_usd` 理解成某个交易所的 venue 成交量
- public beta 部署会跟踪 `data/history/YYYY-MM-DD/*.csv` 历史快照，供 Streamlit Community Cloud 首次启动时重建只读 SQLite query layer

## Run The Pipeline

最常用的一条本地 pipeline：

```bash
python src/transform/clean_watchboard.py
python src/transform/enrich_watchboard_coingecko.py
python src/transform/label_rwa_tokens.py
python src/ingestion/fetch_venue_ticker_metrics.py
python src/quality/audit_watchboard_quality.py
python src/transform/archive_daily_snapshot.py --overwrite
python src/transform/build_history_store.py
```

### Listing Detection

Hyperliquid / multi-venue listing monitor：

```bash
python src/ingestion/hl_listing_monitor.py snapshot --venue all
python src/ingestion/hl_listing_monitor.py daily-summary --venue all
python src/ingestion/hl_listing_monitor.py poll --venue all
```

行为说明：
- 首次运行会初始化 `data/raw/known_listings.json`
- 首次运行不发告警
- 后续运行检测新增 listings 并推送 Lark
- `snapshot --venue all` 会做一次性 listing state / raw watchboard 刷新，不推送 Lark
- `poll --venue all` 会在一个进程里顺序检查各 venue，避免多个进程并发写同一状态文件

### Cleaning

```bash
python src/transform/clean_watchboard.py
```

输入 / 输出：
- 输入：`data/raw/listing_watchboard.csv`
- 输出：`data/processed/listing_watchboard_clean.csv`

### CoinGecko Enrichment And Leaderboards

```bash
python src/transform/enrich_watchboard_coingecko.py
```

输出：
- `data/processed/token_market_metrics.csv`
- `data/processed/listing_watchboard_token_metrics.csv`
- `data/marts/top_volume_tokens.csv`
- `data/marts/top_gainers_tokens.csv`
- `data/marts/top_losers_tokens.csv`
- `data/marts/hot_new_tokens.csv`
- `data/audits/token_market_match_audit.csv`
- `data/audits/token_market_metrics_audit.csv`

### RWA Labeling

```bash
python src/transform/label_rwa_tokens.py
```

输出：
- `data/processed/token_rwa_labels.csv`
- `data/processed/token_rwa_review_queue.csv`
- `data/cache/coingecko_coin_details_cache.json`

V1 规则：
- 主键优先使用 `coingecko_id`，不依赖 symbol 作为唯一分类键
- 优先级严格为：
  - `manual_override`
  - `seed_allowlist`
  - `cached_coingecko_categories`
  - `conservative_keyword_fallback`
- CoinGecko detail cache 只会对前两层都未命中的 coin ID 拉取并缓存
- public CoinGecko 额度较紧时，detail cache 会按小批量渐进预热；一旦确认持续 `429`，本轮会停止继续拉取并直接落地标签结果
- 主流稳定币默认排除为 `non_rwa`
- 证据冲突或边界模糊时使用 `review_pending`
- `token_rwa_review_queue.csv` 只聚焦 `review_pending`，并优先按 `24h volume`、`market cap`、再按是否存在 keyword/category 证据排序，便于运营先看高价值待复核 token

当前 `config/rwa_allowlist.csv` schema：
- `coingecko_id`
- `rwa_label`
- `rwa_category`
- `protocol`
- `force_override`
- `notes`

### Venue Ticker Metrics

```bash
python src/ingestion/fetch_venue_ticker_metrics.py
```

输出：
- `data/processed/venue_ticker_metrics.csv`

当前 resilience 行为：
- 每个 venue 最多重试 3 次，按 `1s / 2s / 4s` exponential backoff
- 单个 venue 最终失败时，不会中断整条 ticker pipeline；其他 venue 继续处理
- 如果本地已有上一份成功的 `venue_ticker_metrics.csv`，失败 venue 会优先复用上一份该 venue 的 rows，并标记为 stale fallback
- `venue_ticker_metrics.csv` 会额外写出：
  - `fetch_status`
  - `snapshot_time`
  - `data_freshness`
  - `source_error`

### Quality Audit

```bash
python src/quality/audit_watchboard_quality.py
```

输出：
- `data/audits/listing_coverage_audit.csv`
- `data/audits/token_market_metrics_audit.csv`

## Daily Snapshot And SQLite Query Layer

归档当前日输出：

```bash
python src/transform/archive_daily_snapshot.py --overwrite
```

会复制当前主要结果到：
- `data/history/YYYY-MM-DD/`

构建 SQLite query layer：

```bash
python src/transform/build_history_store.py
```

SQLite 文件：
- `data/db/listing_watchboard_history.sqlite`

当前表层次：
- `listing_snapshots`
- `token_market_metrics_daily`
- `token_rwa_labels_daily`
- `venue_ticker_metrics_daily`
- `token_metrics_daily`
- `leaderboard_daily`

RWA 查询例子：

```sql
-- 最新一版 review queue
SELECT
  snapshot_date,
  token,
  coingecko_id,
  rwa_label,
  rwa_category,
  confidence,
  label_source
FROM token_rwa_labels_daily
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM token_rwa_labels_daily)
  AND rwa_label = 'review_pending'
ORDER BY confidence ASC, token ASC;
```

```sql
-- 某天的 core / related RWA token
SELECT
  l.snapshot_date,
  l.token,
  l.coingecko_id,
  l.rwa_label,
  l.rwa_category,
  l.protocol,
  t.venue_count,
  t.volume_24h_usd
FROM token_rwa_labels_daily l
LEFT JOIN token_metrics_daily t
  ON l.snapshot_date = t.snapshot_date
 AND l.token = t.token
WHERE l.snapshot_date = '2026-04-15'
  AND l.rwa_label IN ('core', 'related')
ORDER BY l.rwa_label, t.volume_24h_usd DESC, l.token ASC;
```

人工复核工作流：
- 每天优先查看 `token_rwa_labels_daily` 中 `rwa_label = 'review_pending'` 的 token
- 核对对应 `coingecko_id`、CoinGecko categories、项目描述与官网定位
- 如果结论明确，把 coin ID 写入 `config/rwa_allowlist.csv`
- 若必须强制纠偏，设置 `force_override = true`

## Lark Delivery

推送每日卡片：

```bash
python src/delivery/lark_listing_watchboard.py
```

如果临时覆盖 webhook：

```bash
python src/delivery/lark_listing_watchboard.py --webhook "https://open.larksuite.com/open-apis/bot/v2/hook/another-webhook"
```

卡片当前聚焦：
- `New Listings 24h`
- `Hot New Tokens`
- `Top Volume 24h`
- `Top Movers 24h`

并明确区分：
- Token Market View = CoinGecko token-level aggregated market data
- Venue Perp View = exchange-specific perp/swap/futures metrics

## Streamlit Dashboard

启动本地 dashboard：

```bash
python3 -m streamlit run src/app/streamlit_app.py
```

或用 Makefile：

```bash
make ui
```

主要页面：
- `Overview`
- `Token Drill-down`
- `Venue View`
- `History / Diff`
- `Data Quality`

常用 deep links：

```text
http://localhost:8511/?page=overview&snapshot=2026-04-14
http://localhost:8511/?page=token&snapshot=2026-04-14&token=SUI
http://localhost:8511/?page=venue&snapshot=2026-04-14&venue=binance
http://localhost:8511/?page=history&snapshot=2026-04-14&token=SUI
http://localhost:8511/?page=quality&snapshot=2026-04-14
```

### Share Demo On Local Network

如果你想在同一个局域网里给同事演示：

```bash
make ui-lan
```

然后让同事打开：

```text
http://<your-lan-ip>:8511
```

注意：
- 同事需要和你在同一个 LAN / Wi‑Fi 网络里
- macOS / Windows 防火墙可能需要允许 `8511` 入站连接

## Public Beta Deployment

最简单的 public beta 路径是：

```text
GitHub repo -> Streamlit Community Cloud
```

当前这个 repo 已经按这个路径做了最小部署准备：
- app 入口：`src/app/streamlit_app.py`
- 依赖：`requirements.txt`
- public beta 数据源：跟踪提交的 `data/history/YYYY-MM-DD/*.csv`
- cloud 首次启动时，app 会从 `data/history/` 自动重建本地只读 SQLite query layer

部署到 Streamlit Community Cloud 时：
- Repository: 选择这个 GitHub repo
- Branch: 选择你要部署的分支
- Main file path: `src/app/streamlit_app.py`
- Secrets: 当前 public beta **不需要**

说明：
- 不需要提交 `.env`
- 不需要提交 `.streamlit/secrets.toml`
- 不建议提交 `data/cache/`、`data/db/`、本地 SQLite、日志文件
- 为了让 public beta 页面在云端直接可看，repo 会保留少量 `data/history/*.csv` 快照作为只读展示数据
- 这是一条最小 beta 路线，不是生产级数据平台

## Backward Compatibility Notes

- 旧的根目录脚本路径已经迁移到 `src/...`
- 旧的数据文件路径已经迁移到 `data/...`
- `data/processed/listing_watchboard_enriched.csv` 作为 legacy 输出保留，但不再是主要 source of truth
- 如果你之前有手工脚本或 cron 指向旧路径，需要改成新的 `src/...` 命令

## Git Hygiene

推荐纳入版本管理的内容：
- `README.md`
- `.gitignore`
- `.env.example`
- `Makefile`
- `config/`
- `src/`
- `data/*/.gitkeep`
- 其他代码、配置、文档类文件

不建议提交：
- `.env`
- 其他 `.env.*` secrets 文件
- `.streamlit/secrets.toml`
- `*.sqlite` / `*.db` 本地数据库
- `*.log`、`logs/`、以及其他本地运行日志
- `data/raw/*`
- `data/cache/*`
- `data/processed/*`
- `data/marts/*`
- `data/audits/*`
- `data/db/*`
- 本地 IDE / Python 缓存目录，如 `.vscode/`、`.idea/`、`__pycache__/`、`.venv/`

public beta 的一个例外：
- 可以提交少量 `data/history/YYYY-MM-DD/*.csv` 快照，作为 Streamlit Community Cloud 的只读展示数据来源

推荐初始化方式：

```bash
git init
git add README.md .gitignore .env.example Makefile config src \
  data/raw/.gitkeep data/processed/.gitkeep data/marts/.gitkeep \
  data/audits/.gitkeep data/history/.gitkeep data/db/.gitkeep
git commit -m "Initial listing monitor pipeline structure"
```

首次推送到远程：

```bash
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```
