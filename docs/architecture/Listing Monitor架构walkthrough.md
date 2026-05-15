# Listing Monitor — Comprehensive Project Review

---

## Part 1: Executive Summary（项目全景理解）

### 核心价值主张
Listing Monitor 解决的是 **"cross-exchange perp listing intelligence"** 问题——帮助加密货币交易者与机构用户在第一时间感知**哪些币种在哪些交易所上了永续合约**，以及这些新上的币种的市场表现如何。目标用户是：交易员（套利 / market making）、研究分析师、以及交易所运营团队（竞品对标）。

### 技术栈与架构
- **语言**：纯 Python（~7,000 行 src）
- **存储层**：CSV 文件 + SQLite（文件驱动，zero infrastructure）
- **前端**：Streamlit dashboard，5 个页面
- **推送**：Lark (Feishu) webhook rich-card 消息
- **外部 API**：7 家交易所 REST API + CoinGecko
- **模式**：**分层 batch pipeline** — 典型的 ETL with a thin presentation layer

### 数据流向

```
交易所 REST APIs (7 venues)              CoinGecko API
        │                                      │
    ┌───▼────────────┐                 ┌───────▼──────────┐
    │   Ingestion    │                 │  Enrichment      │
    │ hl_listing_    │                 │  enrich_watch-   │
    │ monitor.py     │                 │  board_coingecko │
    └───┬────────────┘                 └───────┬──────────┘
        │                                      │
   raw/known_listings.json              processed/token_market_metrics.csv
   raw/listing_watchboard.csv            marts/top_*.csv, hot_new_*.csv
        │                                      │
    ┌───▼────────────┐     ┌───────────────────▼──────────┐
    │  Transform     │     │  RWA Labeling                │
    │  clean_watch-  │     │  label_rwa_tokens.py          │
    │  board.py      │     │  (allowlist + CoinGecko       │
    └───┬────────────┘     │   categories + keywords)      │
        │                  └───────────────┬──────────────┘
   processed/listing_                processed/token_rwa_labels.csv
   watchboard_clean.csv              processed/token_rwa_review_queue.csv
        │                                      │
    ┌───▼────────────────────────────────────▼──┐
    │  Archive → SQLite History Store           │
    │  archive_daily_snapshot.py                 │
    │  build_history_store.py                    │
    └───┬───────────────────────────────────────┘
        │
   data/db/listing_watchboard_history.sqlite
        │
    ┌───▼────────────┐          ┌─────────────────┐
    │  Streamlit     │          │  Lark Delivery   │
    │  Dashboard     │          │  (rich card)     │
    └────────────────┘          └─────────────────┘
```

### 当前成熟度：**6.5 / 10**（Strong MVP，approaching production-lite）

**为什么是 6.5：**
- ✅ Pipeline 层次清晰，从 raw → clean → enriched → labeled → archived 有完整链路
- ✅ RWA labeling 做得非常有规划性——多层优先级、confidence scoring、review queue、evidence trail
- ✅ 有 data quality audit、stale fallback 机制、error handling with retry
- ✅ README 文档水准远超一般 side project
- ❌ 测试覆盖率很低（只有 1 个 test file，6 个 test cases）
- ❌ 没有 `requirements.txt` / `pyproject.toml`
- ❌ 跨模块的工具函数（`clean_text`, `format_number`, `log`）大量重复
- ❌ 缺少 CI/CD、linting 配置
- ❌ 没有 scheduling / orchestration 层

---

## Part 2: Code Quality Deep Dive

### 2.1 代码组织与可维护性

```
评分：7.5 / 10
```

**优点：**
1. **清晰的 pipeline 分层**：`ingestion / transform / quality / delivery / app / common` 职责划分合理，基本遵循 data engineering 的 ELT 惯例
2. **`src/common/paths.py` 是亮点**：所有路径集中管理，完全消除了硬编码路径问题，任何新模块只需 `from src.common.paths import XXX` 即可
3. **`config/` 与代码分离**：`rwa_allowlist.csv` 和 `coingecko_overrides.py` 让运营人员可以独立维护 override 配置，不需要改代码

**问题（按优先级）：**

1. **`clean_text()` / `format_number()` / `log()` 在 8 个文件中各自重复实现** — 这是最大的代码卫生问题。每个模块都有自己的 `clean_text()`，签名完全一致。这些应该统一到 `src/common/utils.py`。

   涉及文件：`hl_listing_monitor.py`, `fetch_venue_ticker_metrics.py`, `clean_watchboard.py`, `enrich_watchboard_coingecko.py`, `label_rwa_tokens.py`, `audit_watchboard_quality.py`, `lark_listing_watchboard.py`, `streamlit_app.py`

2. **`hl_listing_monitor.py` 过于庞大（1,175 行）** — 它既是 venue fetcher 又是 state manager 又是 Lark formatter 又是 CLI entrypoint。建议拆分为：
   - `src/ingestion/venue_fetchers.py` — 7 个 `fetch_*_listings()` 函数
   - `src/ingestion/listing_state.py` — state load/save/merge 逻辑
   - `src/ingestion/listing_monitor.py` — CLI + orchestration

3. **没有依赖管理文件** — 没有 `requirements.txt`、`pyproject.toml`、或 `setup.py`。新人 clone 下来不知道该安装什么版本。README 虽然写了 `pip install requests python-dotenv pandas streamlit`，但缺少版本锁定。

**Quick Win：**
- 立即创建 `src/common/utils.py` 并提取公共函数（~30 min）
- 新建 `requirements.txt` 锁定依赖版本（~10 min）

---

### 2.2 数据质量和健壮性

```
评分：8 / 10
```

**优点：**
1. **Retry + exponential backoff 做得正规**：`fetch_venue_ticker_metrics.py` 有 3 次重试带指数退避；CoinGecko 调用在 429 时有 `Retry-After` header 感知和指数退避
2. **Stale fallback 机制非常成熟**：venue ticker 失败时会复用上一次成功的数据，并标注 `fetch_status=fallback_reused` + `data_freshness=stale_fallback`，downstream 完全可追溯
3. **RWA labeling 的 `validate_output_rows()` 是良好的 data contract 实践** — 验证 label 值域、confidence 范围、label_source 非空

**问题：**

1. **`hl_listing_monitor.py` 的 venue fetcher 没有 retry** — 与 `fetch_venue_ticker_metrics.py` 的 retry 设计不一致。如果 Bybit API 暂时 500，listing state 更新就会中断。

   ```python
   # 当前：直接 raise，没有 retry
   def fetch_bybit_listings() -> dict:
       response = requests.get(BYBIT_INSTRUMENTS_URL, params=params, timeout=10)
       response.raise_for_status()  # ← no retry
   ```

   > 💡 **建议**：把 `fetch_venue_with_retry()` 模式从 ticker metrics 提取到 `src/common/http.py` 作为通用装饰器

2. **CoinGecko rate limiting 策略可以更优雅** — 当前 `STOP_AFTER_CONSECUTIVE_429 = 1` 意味着一碰到 429 就立刻停止。在免费 API 额度紧张时这合理，但考虑 demo key 的 30 req/min，可以用 token bucket / sliding window 来最大化利用额度

3. **日志缺少结构化** — 所有模块用 `print()` + 自定义前缀。对 local dev 够用，但如果要排查历史问题或推到生产就需要 `logging` module + 结构化输出

**Quick Win：**
- 给 `hl_listing_monitor.py` 的 7 个 fetcher 加上统一的 retry wrapper（~1h）

---

### 2.3 性能和可扩展性

```
评分：7 / 10
```

**优点：**
1. **CoinGecko enrichment 做了 chunked batch + ID 去重**：`chunked()` 按 1000 个 ID 一批请求，减少 API 调用次数
2. **SQLite history store 有合理的 index 设计**：针对常见 dashboard scenario（token lookup, venue 过滤, snapshot 日期）有复合索引

**问题：**

1. **所有 venue ticker fetch 是串行的** — `fetch_venue_ticker_metrics.py` 顺序遍历 5 个交易所。如果用 `concurrent.futures.ThreadPoolExecutor` 并行化，总延迟从 ~30s 降到 ~10s（取决于最慢的交易所）

   ```python
   # 当前：串行
   for venue, fetcher in venue_fetchers:
       venue_rows.extend(fetch_venue_with_retry(...))
   
   # 建议：并行
   with ThreadPoolExecutor(max_workers=5) as executor:
       futures = {executor.submit(fetch_venue_with_retry, ...): venue for venue, fetcher in venue_fetchers}
   ```

2. **`build_history_store.py` 每次 DROP + 重建所有表** — 这在数据量小时没问题，但积累 180 天 × 7 venues × 500 symbols 后，全量重建会越来越慢。建议改为 incremental upsert（只处理新的 snapshot 日期）

3. **Streamlit dashboard 每次页面切换都会重新执行 SQL 查询** — 没有使用 `@st.cache_data` 装饰器。对于 snapshot_dates、leaderboard 这类短时间内不会变的数据，应该加缓存

**Quick Win：**
- 给 Streamlit 的 `wq.snapshot_dates()` 和 `wq.snapshot_summary()` 加 `@st.cache_data(ttl=300)`（~20 min）

---

### 2.4 测试和文档

```
评分：5.5 / 10
```

**优点：**
1. **README 质量优秀** — 459 行，覆盖架构、运行方式、语义约定、SQL 示例、Git hygiene。这是很多生产项目都做不到的水准
2. **`test_label_rwa_tokens.py` 测试用例设计合理** — 覆盖了 manual override > seed allowlist > cache > keyword 的优先级链路

**问题：**

1. **只有 1 个 test file，6 个 test cases** — 约 7,000 行代码只有 226 行测试（~3.2% 覆盖率）。关键未测模块：
   - `hl_listing_monitor.py`（state management 逻辑）
   - `clean_watchboard.py`（timestamp 解析）
   - `enrich_watchboard_coingecko.py`（symbol 匹配逻辑）
   - `build_history_store.py`（schema migration）

2. **没有 `requirements.txt` 或 `pyproject.toml`** — 新人无法确定精确依赖

3. **缺少 data schema 文档** — 虽然每个脚本 docstring 写了 input/output 文件名，但没有集中的 field-level schema 定义。建议在 `docs/data_dictionary.md` 中维护

**Quick Win：**
- 创建 `requirements.txt`（~5 min）
- 给 `clean_watchboard.py` 加 5-10 个 unit tests 覆盖 timestamp parsing edge cases（~1h）

---

## Part 3: Architecture Review

### 3.1 当前架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                              │
│  Hyperliquid │ Binance │ Bybit │ OKX │ Bitget │ dYdX │ Drift   │
│                     +  CoinGecko API                             │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                      INGESTION LAYER                             │
│  hl_listing_monitor.py        fetch_venue_ticker_metrics.py      │
│  (poll/snapshot/daily-summary)  (venue-specific ticker snapshot)  │
│                                                                  │
│  State: data/raw/known_listings.json                             │
│  Output: data/raw/listing_watchboard.csv                         │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                      TRANSFORM LAYER                             │
│                                                                  │
│  clean_watchboard.py          → listing_watchboard_clean.csv     │
│  enrich_watchboard_coingecko  → token_market_metrics.csv         │
│                               → marts/top_*.csv, hot_new_*.csv   │
│  label_rwa_tokens.py          → token_rwa_labels.csv             │
│                               → token_rwa_review_queue.csv       │
│  archive_daily_snapshot.py    → history/YYYY-MM-DD/              │
│  build_history_store.py       → db/SQLite                        │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                      QUALITY LAYER                               │
│  audit_watchboard_quality.py                                     │
│  → listing_coverage_audit.csv                                    │
│  → token_market_metrics_audit.csv                                │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                   DELIVERY / PRESENTATION                        │
│                                                                  │
│  Lark Delivery                    Streamlit Dashboard            │
│  lark_listing_watchboard.py       streamlit_app.py               │
│  (rich card via webhook)          watchboard_query.py             │
│                                   (SQLite query helpers)         │
└──────────────────────────────────────────────────────────────────┘

TRIGGER：手动 `make all` / `make daily` / cron
```

### 3.2 架构优缺点

**最大优点：Zero infrastructure dependency**

整个系统只需 Python + 文件系统。没有 Kafka、没有 Airflow、没有 Postgres。这个选择对于当前阶段（个人/小团队、daily batch）是**非常正确的 trade-off**。你可以在任何笔记本上 clone、安装依赖、`make all` 就跑起来。

**最大技术债：`hl_listing_monitor.py` 的 God Object 模式**

1,175 行代码承担了太多职责。它既管 7 个交易所的 API 调用，又管 state load/save，又管 Lark 消息格式化，又管 CLI 参数解析。任何一处改动都容易引入意外 regression。

**如果重构，我会改这 3 个地方（按 ROI 排序）：**

| 优先级 | 重构项 | 为什么 | 预计收益 |
|--------|--------|--------|----------|
| 1 | 提取 `src/common/utils.py` | 消除 8 个文件中的重复工具函数 | 减少 ~200 行重复代码，统一行为 |
| 2 | 拆分 `hl_listing_monitor.py` | 把 1175 行拆成 3-4 个文件 | 可独立测试 fetcher/state/alert |
| 3 | 把 `build_history_store.py` 改为增量模式 | 避免每天全量 DROP + 重建 | 随数据增长保持性能 |

### 3.3 可扩展性分析

#### 场景 1：新增 5 个交易所

**改动量评估：中等（每个交易所 ~40-80 行）**

当前的 venue 扩展模式非常好——`VENUES` 字典 + `fetch_*_listings()` 函数是干净的 strategy pattern。新增 Kraken 的步骤：

```python
# 1. 在 hl_listing_monitor.py 里加一个 fetcher
def fetch_kraken_listings() -> dict:
    ...

# 2. 在 VENUES dict 里注册
VENUES["kraken"] = {
    "display_name": "Kraken",
    "market_label": "perp",
    "listings_url": "https://...",
    "fetch_listings": fetch_kraken_listings,
}

# 3. 在 fetch_venue_ticker_metrics.py 里加 ticker fetcher
# 4. 在 lark 和 streamlit 的 VENUE_LABELS 里加 display name
```

> ⚠️ **痛点**：`VENUE_LABELS` 在 3 个文件中重复定义（enrich、lark、streamlit）。这应该统一到 `src/common/venues.py`。

#### 场景 2：RWA 币种分类功能（你老板的需求）

**当前架构已优雅支持 ✅**

RWA labeling 已经是一个独立的 transform step，插入点清晰：

```
clean → enrich (CoinGecko) → ★ label_rwa_tokens ★ → tickers → audit → archive → db
```

**我的建议设计扩展方向：**

1. **扩充 `config/rwa_allowlist.csv`** — 当前有 17 条 entries，先把已知的 RWA 项目补齐到 50-100 条
2. **增加 CoinGecko categories rule** — 在 `CORE_CATEGORY_RULES` 和 `RELATED_CATEGORY_RULES` 里添加新的分类规则（如 `tokenized-equity`、`private-credit`）
3. **考虑接入 RWA.xyz API** — 作为第三数据源，在 allowlist 和 CoinGecko 之间插入一层

**代码改动量：轻量。** 当前 `label_rwa_tokens.py` 的 4 层优先级架构（manual_override → seed_allowlist → cached_coingecko_categories → keyword_fallback）本身就是 extensible 的，你可以在 layer 2 和 3 之间插入 `rwa_xyz_api_match` 层。

#### 场景 3：实时监控

**架构改动程度：中到大**

需要的改变：
1. **Ingestion 层**：从 30-min polling 改为 WebSocket 或 5-sec polling 短周期
2. **State 管理**：从 JSON file 改为 Redis / in-memory state（文件 I/O 无法支撑秒级更新）
3. **推送层**：从 batch Lark card 改为 event-driven 即时消息
4. **不需要改的**：`clean / enrich / label` 逻辑可以保持 batch，因为 RWA 标签不需要实时更新

建议的过渡方案：
```
当前: cron → make all (30min+)
中期: cron → make listings (5min) + make pipeline (30min)
远期: dedicated listing poller process + async pipeline trigger
```

---

## Part 4: Product Perspective

### 4.1 功能完整性

**当前 feature set 对 "internal intelligence tool" 够用。** 你已经覆盖了：
- ✅ 多交易所 listing 检测
- ✅ Token-level 和 venue-level 市场数据
- ✅ Top volume / gainers / losers / hot new 排行榜
- ✅ RWA 分类（multi-layer classification）
- ✅ 历史对比（snapshot diff）
- ✅ Data quality audit

**Obvious missing features：**
1. **Alert 规则自定义** — 目前只推送 "new listing"，但用户可能想设置 "notify me when token X is listed on exchange Y"
2. **Delisting 检测** — 你检测到了 `added` 和 `removed`，但 removed 没有推送 alert
3. **Token 搜索 / 过滤** — Dashboard 面向 token 和 venue 分开的，缺少 "show me all tokens listed on 3+ venues in the last 7 days" 这种组合查询
4. **API for integration** — 如果要让外部系统消费这个数据，需要 REST API 而不只是 Streamlit

**如果只能加 1 个新功能：**

> 💡 **Cross-exchange listing race tracker** — "Token X 第一个在 Bybit 上永续，3 天后 Binance 跟上，7 天后 OKX 也上了"。这个 insight 对交易员和交易所运营都有独特价值，而且你的数据已经支持（`earliest_listing_time_utc` + `venue_count` + expansion history），只缺一个好看的 visualization。

### 4.2 用户体验

**Streamlit 界面评价：7/10**

做得好的：
- 5 个页面分工合理（Overview / Token / Venue / History / Quality）
- Deep link 支持（`?page=token&token=SUI`）非常好，方便在 Lark 消息里嵌入链接
- RWA filter 跨页面统一

可以改进的：
- 缺少 **visual hierarchy** — 所有 dataframe 看起来差不多，缺少颜色编码（比如 RWA Core = 绿色 badge, Review Pending = 黄色）
- **Overview 页面信息密度过高** — 新用户不知道先看什么。建议加 "highlights" 区域（比如 "3 new tokens listed in last 24h, SUI expanded to 5 venues"）
- 缺少 **chart/visualization** — 除了 venue expansion line chart 外，大量数据都是表格。一个 "listings per venue" 的 bar chart 或 "volume heatmap" 会大幅提升 "aha moment"

### 4.3 商业价值和差异化

**vs CoinMarketCap / Coinglass listing tracker：**

| 维度 | CMC/Coinglass | Listing Monitor |
|------|---------------|-----------------|
| 数据覆盖 | 广但浅（只有 listing 事件） | 窄但深（listing + market enrichment + RWA classification） |
| RWA 分类 | ❌ | ✅ Multi-layer with confidence scoring |
| 历史对比 | ❌ | ✅ Day-over-day snapshot diff |
| Cross-exchange listing race | ❌ | ✅ Venue expansion tracking |
| 自定义推送 | 有限 | Lark rich card with deep links |
| 数据质量审计 | ❌ | ✅ Code-level audit trail |

**如何 pitch 这个项目：**

> "我们的优势不是列出新上的币——任何人都能做到。我们的优势是**分析上币行为背后的信号**：哪些交易所是 leader vs follower？哪些新上的币是 RWA asset vs meme coin？一个币从第一家上线到覆盖 5 家交易所平均需要多久？这些 insights 是 CoinMarketCap 没有的。"

**Monetization 机会：**
1. **Institutional API** — 卖给做市商 / 量化团队作为信号源
2. **RWA Intelligence Report** — 定期输出 RWA listing trends report（结合你的行业背景）
3. **Exchange partnership** — 帮交易所做竞品 listing 对标分析

---

## Part 5: Actionable Recommendations

### P0 — Critical（必须修复）

**1. 添加依赖管理文件**

影响：新人无法复现环境；部署到任何服务器都有版本冲突风险
解决方案：
```bash
# 创建 requirements.txt
pip freeze > requirements.txt

# 或更好的：创建 pyproject.toml
```

```toml
# pyproject.toml 示例
[project]
name = "listing-monitor"
version = "0.3.0"
requires-python = ">=3.10"
dependencies = [
    "requests>=2.31",
    "python-dotenv>=1.0",
    "pandas>=2.0",
    "streamlit>=1.30",
]
```

预计工作量：30 min

---

**2. `hl_listing_monitor.py` venue fetcher 缺少 retry**

影响：任何一个交易所 API 暂时故障，整个 listing state 更新就会 exception（poll 模式下会 catch，但 snapshot 模式下 single venue failure 可能导致该 venue 被跳过但不够可见）

解决方案：把 `fetch_venue_ticker_metrics.py` 里成熟的 retry 模式提取为通用工具

```python
# src/common/http.py
def fetch_with_retry(fetcher, venue_name, max_retries=3, backoff_base=1):
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            return fetcher()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                sleep_seconds = backoff_base * (2 ** attempt)
                log(f"{venue_name}: attempt {attempt + 1} failed, retry in {sleep_seconds}s")
                time.sleep(sleep_seconds)
    raise RuntimeError(f"{venue_name}: all attempts failed. Last error: {last_error}")
```

预计工作量：1h

---

**3. `archive_daily_snapshot.py` 有一个 `PROJECT_ROOT` 重复定义 bug**

影响：目前不影响运行（第二个 `PROJECT_ROOT` 覆盖了第一个，但指向同一个路径），但这是 copy-paste 残留，容易在重构时引入 bug。

```python
# Line 16-17
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # ← 第一次定义
...
# Line 23
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # ← 重复定义，应删除
```

预计工作量：5 min

---

### P1 — 强烈建议（High Impact）

**4. 提取公共工具函数到 `src/common/utils.py`**

影响：减少 ~200 行重复代码，统一行为和 bug 修复路径

```python
# src/common/utils.py
def clean_text(value) -> str: ...
def format_number(value, precision=12) -> str: ...
def numeric_value(value) -> float | None: ...
def parse_datetime(value: str) -> datetime | None: ...
def now_utc() -> datetime: ...
def log(module: str, message: str): ...
```

预计工作量：2h

---

**5. 增加测试覆盖率到关键路径**

影响：目前 6 个 test cases 只覆盖 RWA labeling。最关键的未测逻辑是 state management 和 symbol matching。

建议新增测试：
- `test_clean_watchboard.py` — timestamp parsing, metadata parsing edge cases（~10 tests）
- `test_enrich_coingecko.py` — symbol matching disambiguation logic（~8 tests）
- `test_listing_state.py` — state normalize, merge, venue state migration（~10 tests）

预计工作量：4-6h

---

**6. `build_history_store.py` 改为增量 upsert**

影响：随着历史数据增长（180+ 天），全量 `DROP TABLE + 重建` 会越来越慢

```python
# 改进方案：只处理新 snapshot
def ingest_new_snapshots(connection, snapshot_dirs):
    existing_dates = {row[0] for row in connection.execute(
        "SELECT snapshot_date FROM snapshot_runs"
    ).fetchall()}
    for snapshot_dir in snapshot_dirs:
        if snapshot_dir.name not in existing_dates:
            ingest_snapshot_dir(connection, snapshot_dir)
```

预计工作量：2h

---

**7. 统一 `VENUE_LABELS` 到单一位置**

影响：当前在 `enrich_watchboard_coingecko.py`、`lark_listing_watchboard.py`、`streamlit_app.py` 三处重复定义。新增 venue 时容易忘记更新其中之一。

```python
# src/common/venues.py
VENUE_LABELS = {
    "binance": "Binance",
    "bitget": "Bitget",
    ...
}
```

预计工作量：30 min

---

### P2 — Nice to Have（Enhancement）

**8. Streamlit dashboard 加 `@st.cache_data` 缓存**

预计工作量：30 min

---

**9. Venue ticker fetch 并行化**

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=5) as executor:
    futures = {
        executor.submit(fetch_venue_with_retry, venue, universe[venue], fetcher, previous_output_rows): venue
        for venue, fetcher in venue_fetchers
    }
    for future in as_completed(futures):
        venue_rows.extend(future.result())
```

预计工作量：1h

---

**10. 加入 Delisting 检测与告警**

预计工作量：3h

---

**11. 使用 Python `logging` 替代 `print()`**

预计工作量：2h

---

**12. 添加 Linting / CI 配置**

```bash
# 建议：ruff (fast, all-in-one)
pip install ruff
ruff check src/ tests/
ruff format src/ tests/
```

配合 GitHub Actions：
```yaml
# .github/workflows/lint.yml
name: Lint
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install ruff
      - run: ruff check src/ tests/
```

预计工作量：1h

---

## Part 6: 技术亮点（Highlights）

### 1. 这个项目中做得特别好的设计

**① RWA 分类的 4 层优先级架构**

```
manual_override (0.99 confidence)
    ↓ miss
seed_allowlist (0.95 confidence)
    ↓ miss
cached_coingecko_categories (0.78-0.84 confidence)
    ↓ miss
conservative_keyword_fallback (0.15-0.67 confidence)
```

这个设计体现了 **production ML pipeline 的思维方式**（即使没有用 ML）：有 ground truth override、有 high-confidence seed、有 automated signal、有 conservative fallback，并且每一层都附带 confidence score 和 evidence trail。这比 "if category == RWA: return True" 成熟太多。

**② 文件驱动 + 分层数据目录**

```
data/
  raw/          ← 不可变的 source of truth
  cache/        ← 可重建的 API 缓存
  processed/    ← 清洗后的中间数据
  marts/        ← 面向 dashboard 的 aggregated view
  audits/       ← data quality 审计
  history/      ← 每日快照归档
  db/           ← SQLite query layer
```

这个目录结构直接对应了 dbt-style 的 data warehouse 分层（raw → staging → marts）。对于一个没有用任何 data framework 的项目来说，这个设计非常有正式感。

**③ Venue ticker 的 stale fallback + data provenance**

每一行 ticker 数据都带 `fetch_status`、`data_freshness`、`source_error` 字段。downstream 消费者完全知道自己看到的数据是 fresh 还是 stale、为什么是 stale、stale 到什么程度。这是很多生产级 data pipeline 都做不到的。

### 2. 可抽象为 library 的模式

- **Multi-venue API aggregator pattern** — `VENUES` dict + `fetch_*` function + retry wrapper 可以打包成 `crypto-venue-scraper` library
- **Layered classifier with evidence trail** — RWA labeling 的 allowlist → categories → keywords pattern 可以泛化为 any domain 的 classification pipeline template
- **File-driven pipeline orchestration** — `paths.py` + `Makefile` + archive/rebuild pattern 是轻量级 ETL 的可复用模板

### 3. 面试时应该重点讲的 3 个技术点

| # | 技术点 | 怎么讲 |
|---|--------|--------|
| 1 | **Multi-source data pipeline design** | "我设计了一个分层 ETL pipeline，从 7 家交易所 API 和 CoinGecko 采集数据，通过 raw → clean → enrich → label → archive 的流程处理。关键 trade-off：选择 file-based pipeline 而非 Airflow，因为在 MVP 阶段 zero infra > 可扩展性。" |
| 2 | **RWA classification 的信号融合策略** | "我实现了一个 4 层信号融合分类器：manual override > seed allowlist > CoinGecko categories > keyword fallback，每层有不同的 confidence scoring。关键设计决策是 'conservative by default' — 宁可 review_pending 也不误标。" |
| 3 | **Data quality engineering** | "每个 venue ticker row 都带 fetch_status + data_freshness + source_error 元数据。当 API 失败时，pipeline 不中断而是 fallback to stale data 并标注 provenance。我还加了一个 listing coverage audit 来检测 drop-off 发生在哪个 pipeline stage。" |

---

## Bonus：Technical Roadmap to Production

### 6 个 Milestone

| # | Milestone | 预计时间 | 关键交付物 |
|---|-----------|----------|------------|
| M1 | **Code Hygiene** | 1 week | requirements.txt, src/common/utils.py extraction, VENUE_LABELS unification, PROJECT_ROOT bug fix, ruff linting |
| M2 | **Test Foundation** | 1 week | 30+ unit tests, ~60% coverage on core modules, CI pipeline |
| M3 | **Performance & Incremental** | 1 week | Parallel ticker fetch, incremental SQLite build, Streamlit cache |
| M4 | **RWA Feature v2** | 2 weeks | RWA.xyz API integration, expanded allowlist, RWA-specific dashboard tab, RWA trends report |
| M5 | **Deployment** | 1 week | Docker compose, scheduled runner (cron / GitHub Actions), monitoring / alerting |
| M6 | **API & Open Source** | 2 weeks | REST API layer (FastAPI), API documentation, open source packaging |

### 团队需求（3 个月 launch）

| 角色 | 人数 | 职责 |
|------|------|------|
| Backend Engineer | 1 (可以是你) | Pipeline 优化, API 层, Docker |
| Product/Data Analyst | 0.5 (可以是你) | RWA allowlist 维护, data quality review, stakeholder communication |
| 设计师 | 可选 | Dashboard 视觉优化（如果要开源 / 面向外部用户） |

### 竞争优势总结

你有**两个 CMC/Coinglass 都不做的独特 feature**：
1. **RWA classification with evidence trail** — 目前市场上没有哪个 listing tracker 会自动标注 RWA 并告诉你为什么这么标
2. **Cross-exchange listing race intelligence** — "SUI 首先在 Bybit 上永续，第 3 天 Binance 跟上" 这种 narrative 对金融机构有直接的交易价值

### 下一个最有价值的功能

**RWA Listing Trend Report（RWA 上币趋势报告）**

理由：
- 你已经有 daily RWA label snapshots + venue expansion history
- RWA 是 2024-2026 年最热的 crypto narrative 之一
- 一份 "本月上了哪些 RWA token、在哪些交易所、市场表现如何" 的自动化报告，可以直接作为你在新公司的 killer demo
- 数据已经全部 ready（`token_rwa_labels_daily` + `token_metrics_daily` + `leaderboard_daily`），只差 aggregation 和 presentation
