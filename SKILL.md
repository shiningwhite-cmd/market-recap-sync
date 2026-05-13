---
name: market-recap-sync
description: 基于 GitHub 的市场记忆系统：盘后将复盘内容结构化写入长期记忆，早盘按日期窗口与主题条件高效召回历史记忆，辅助次日预测与决策。
---

# Market Memory Sync

当用户希望把复盘沉淀为可持续增长的“GitHub 记忆库”，并在次日开盘前读取历史模式时，使用这个 skill。

## 触发场景

- 用户提到“盘后复盘入库/保存历史记忆”
- 用户提到“早盘读取过去记忆做预测/决策辅助”
- 用户提到“基于 GitHub 做长期可追溯的记忆系统”

## 目标能力

该 skill 提供两个核心能力：

1. `ingest`（盘后写入）
- 将输入复盘标准化为统一 schema
- 写入 GitHub 仓库中的长期记忆分层存储
- 自动更新索引并 commit + push

2. `recall`（早盘读取）
- 在指定日期窗口读取历史记忆
- 支持按 `market/themes/tags/symbols` 过滤
- 产出机器可读 JSON + 人类可读 Markdown 摘要

## 长期扩展的数据架构

采用三层存储，兼顾可追溯性、写入效率、读取效率：

1. 日快照层（`daily/`）
- 路径：`memory/daily/YYYY/YYYY-MM/YYYY-MM-DD.json`
- 存放当日完整规范化数据（覆盖同日旧版本）
- 作为最终“真值数据”供精确读取

2. 月度索引层（`indexes/monthly/`）
- 路径：`memory/indexes/monthly/YYYY-MM.json`
- 存放轻量索引项（date/themes/tags/symbols/bias/summary_short/snapshot_path）
- 早盘召回先查该层，不扫全量快照

3. 事件日志层（`journal/`）
- 路径：`memory/journal/YYYY/YYYY-MM.jsonl`
- 追加式写入 upsert 事件（带 digest）用于审计追踪
- 为后续回放、纠错、离线分析提供基础

附加元数据：
- `memory/indexes/calendar.json`：记录已有月份
- `memory/latest.json`：最新一期指针
- `memory/_meta/schema_version.json`：版本元信息

## 为什么这个结构更高效

- 写入复杂度稳定：盘后仅改动“当日快照 + 当月索引 + 当月日志”，避免全量重写。
- 读取按月分片：`lookback` 只需加载命中的月份索引文件，不需要扫描全仓历史文件。
- 索引去重与反规范化：月索引保留主题、标签、标的符号，过滤在索引层完成，大幅减少快照加载数量。
- Git 友好：分片后单文件增长可控，冲突面小，适合长期 commit 历史。

## 必要环境变量

- `MARKET_RECAP_GITHUB_URL`：目标仓库 URL（必填）

## 常用可选环境变量

- `GITHUB_TOKEN`：若 URL 无认证信息，自动注入 token
- `MARKET_RECAP_BRANCH`：默认 `main`
- `MARKET_RECAP_TARGET_DIR`：默认 `memory`
- `MARKET_RECAP_REPO_DIR`：默认 `/tmp/market-memory-repo`
- `GIT_AUTHOR_NAME`：默认 `market-memory-bot`
- `GIT_AUTHOR_EMAIL`：默认 `market-memory-bot@local`

## 执行方式

盘后写入（正式入库）：

```bash
python3 scripts/memory_sync.py ingest --input /path/to/post_market_recap.json
```

盘后只本地生成（不推 GitHub）：

```bash
python3 scripts/memory_sync.py ingest \
  --input /path/to/post_market_recap.json \
  --date 2026-05-13 \
  --only-generate
```

早盘读取最近 60 天：

```bash
python3 scripts/memory_sync.py recall \
  --as-of 2026-05-14 \
  --lookback-days 60 \
  --limit 30
```

按主题和标的过滤读取：

```bash
python3 scripts/memory_sync.py recall \
  --as-of 2026-05-14 \
  --lookback-days 120 \
  --themes AI算力,电力设备 \
  --symbols 600000.SH,300750.SZ
```

## 输入规范建议

- 推荐 JSON 输入
- 最小字段：`date` + `summary`
- 完整模板：`references/input_template.json`
- 统一 schema：`references/canonical_schema.json`

## 读取输出

`recall` 默认输出：

- `output-memory/recall.json`：结构化召回结果（含统计）
- `output-memory/recall.md`：早盘可直接阅读的摘要

可通过 `--output`、`--output-md` 覆盖路径。

## 目录示例

```text
memory/
  _meta/
    schema_version.json
  latest.json
  daily/
    2026/
      2026-05/
        2026-05-13.json
  indexes/
    calendar.json
    monthly/
      2026-05.json
  journal/
    2026/
      2026-05.jsonl
```

## 与旧流程兼容

- 旧脚本 `scripts/recap_sync.py` 仍可用（偏“日报产物同步”）。
- 新脚本 `scripts/memory_sync.py` 用于“长期记忆读写系统”。
- 若希望统一迁移，使用 `scripts/migrate_recaps_to_memory.py` 将历史 `recaps/YYYY/.../recap.json` 批量迁移到 `memory/`。

## 历史迁移（Legacy -> Memory）

先预览迁移计划（不写入）：  

```bash
python3 scripts/migrate_recaps_to_memory.py \
  --source-root /path/to/legacy_repo \
  --recaps-dir recaps \
  --dry-run
```

执行正式迁移（自动 commit + push）：  

```bash
python3 scripts/migrate_recaps_to_memory.py \
  --source-root /path/to/legacy_repo \
  --recaps-dir recaps
```

常用迁移控制参数：

- `--from-date 2025-01-01 --to-date 2025-12-31`：按日期窗口迁移
- `--market CN-A`：只迁移指定市场
- `--themes AI算力,电力设备`：只迁移包含指定主题的数据
- `--overwrite`：覆盖已存在同日期快照
- `--no-push`：仅本地 commit，不推送远端
