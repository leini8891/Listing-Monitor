# Listing Monitor

一个轻量、文件驱动的 perp listing intelligence 项目。

现在的结构已经按 pipeline 分层：
- `src/ingestion/`：交易所抓取与 listing 检测
- `src/transform/`：清洗、CoinGecko enrich、归档、SQLite query layer 构建
- `src/quality/`：数据质量审计
- `src/delivery/`：Lark 推送层
- `src/app/`：Streamlit dashboard
- `config/`：显式配置，如 CoinGecko override map
- `data/`：raw / processed / marts / audits / history / db

## Directory Layout

```text
listing-monitor/
  README.md
  .env
  .env.example
  .gitignore

  config/
    coingecko_overrides.py

  src/
    ingestion/
      hl_listing_monitor.py
      fetch_venue_ticker_metrics.py
    transform/
      clean_watchboard.py
      enrich_watchboard_coingecko.py
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
    processed/
    marts/
    audits/
    history/
    db/
```

## Environment

安装依赖：

```bash
pip install requests python-dotenv pandas streamlit
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

## Makefile

常用入口已经收进 `Makefile`：

```bash
make listings
make clean
make market
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
- `make pipeline`：`clean + market + tickers + metrics + audit + archive + db`
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
- `data/processed/listing_watchboard_clean.csv`
- `data/processed/token_market_metrics.csv`
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
- `venue_ticker_metrics.csv` = exchange-specific perp/swap/futures metrics
- 不要把 CoinGecko `volume_24h_usd` 理解成某个交易所的 venue 成交量

## Run The Pipeline

最常用的一条本地 pipeline：

```bash
python src/transform/clean_watchboard.py
python src/transform/enrich_watchboard_coingecko.py
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

### Venue Ticker Metrics

```bash
python src/ingestion/fetch_venue_ticker_metrics.py
```

输出：
- `data/processed/venue_ticker_metrics.csv`

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
- `venue_ticker_metrics_daily`
- `token_metrics_daily`
- `leaderboard_daily`

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
streamlit run src/app/streamlit_app.py
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
- `.streamlit/config.toml` 里提供了一个 LAN 示例配置；如果你的局域网 IP 变化了，需要把 `browser.serverAddress` 改成当前机器的 IP

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
- `*.sqlite` / `*.db` 本地数据库
- `*.log`、`logs/`、以及其他本地运行日志
- `data/raw/*`
- `data/processed/*`
- `data/marts/*`
- `data/audits/*`
- `data/history/*`
- `data/db/*`
- 本地 IDE / Python 缓存目录，如 `.vscode/`、`.idea/`、`__pycache__/`、`.venv/`

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
