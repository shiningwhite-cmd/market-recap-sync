---
name: market-recap-sync
description: 将每日复盘等关键业务数据规范化为可复用结构化格式（JSON/CSV）和可读文档（Markdown），并自动同步到指定 GitHub 仓库。适用于股市复盘系统、策略日报、交易复盘沉淀。
---

# Market Recap Sync

当用户希望把复盘原始数据整理成标准文档，并自动更新到 GitHub 仓库时，使用这个 skill。

## 何时触发

- 用户提到“复盘总结自动整理/归档/沉淀”
- 用户提到“结构化输出（JSON/CSV）+ 文档输出（Markdown）”
- 用户提到“自动推送到 GitHub 仓库”

## 核心架构

该 skill 采用四层流程，确保“可读 + 可计算 + 可追溯 + 可自动化”：

1. 输入层（Input Ingestion）
- 读取 `--input` 指定的 `.json/.txt/.md` 文件
- 对不同字段命名（中英混合）做兼容映射

2. 规范层（Canonical Normalization）
- 转换为统一 schema：见 `references/canonical_schema.json`
- 强制生成标准日期、市场标识、主题摘要、绩效列表、观察列表等字段

3. 产物层（Artifact Rendering）
- 产出人类可读文档：`recap.md`
- 产出可复用数据：`recap.json`、`performance.csv`、`watchlist.csv`
- 目录结构固定为：`YYYY/YYYY-MM/YYYY-MM-DD/`

4. 发布层（GitHub Sync）
- 基于环境变量获取仓库地址并自动 `clone/pull`
- 将当日产物复制到目标目录（默认 `recaps/`）
- 自动更新 `_index.json` 与 `latest.json`
- 自动 `commit + push`

## 必要环境变量

- `MARKET_RECAP_GITHUB_URL`：目标仓库 URL（必填）
- `GITHUB_TOKEN`：可选。若 URL 无认证信息，脚本会自动注入 token 用于 push

## 可选环境变量

- `MARKET_RECAP_BRANCH`：分支名，默认 `main`
- `MARKET_RECAP_TARGET_DIR`：仓库内目标目录，默认 `recaps`
- `MARKET_RECAP_REPO_DIR`：本地仓库缓存路径，默认 `/tmp/market-recap-sync-repo`
- `GIT_AUTHOR_NAME`：提交作者名（默认 `market-recap-bot`）
- `GIT_AUTHOR_EMAIL`：提交作者邮箱（默认 `market-recap-bot@local`）

## 执行方式

最常用：

```bash
python3 scripts/recap_sync.py --input /path/to/daily_recap.json
```

只生成文档和结构化文件，不同步 GitHub：

```bash
python3 scripts/recap_sync.py --input /path/to/daily_recap.json --only-generate
```

指定输出目录与日期覆盖：

```bash
python3 scripts/recap_sync.py \
  --input /path/to/daily_recap.json \
  --output-dir /tmp/recap-build \
  --date 2026-05-13
```

## 输入规范建议

- 推荐优先使用 JSON 输入
- 最小可用字段：`date` + `summary`
- 完整字段可参考：`references/input_template.json`

如果输入是纯文本，脚本会自动将其落入摘要字段并生成最小可用产物。

## 输出结果说明

生成目录示例（本地构建目录中）：

```text
output/2026/2026-05/2026-05-13/
  recap.json
  recap.md
  performance.csv
  watchlist.csv
```

同步到仓库后，还会维护：

- `recaps/_index.json`：按日期聚合的索引
- `recaps/latest.json`：最新一期指针

## 执行顺序建议

1. 确认输入文件路径和日期
2. 确认环境变量（尤其 `MARKET_RECAP_GITHUB_URL`）
3. 先 `--only-generate` 预览
4. 去掉 `--only-generate` 执行正式同步

