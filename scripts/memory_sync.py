#!/usr/bin/env python3
"""
GitHub-backed market memory system.

Modes:
1) ingest: normalize post-market recap and persist memory into GitHub repo
2) recall: load historical memory for pre-market prediction support

Storage model (scales for long history):
- Daily snapshot: immutable latest daily canonical JSON under daily/YYYY/YYYY-MM/YYYY-MM-DD.json
- Monthly manifest: compact read index under indexes/monthly/YYYY-MM.json
- Append-only journal: audit events under journal/YYYY/YYYY-MM.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MARKET = "CN-A"
DEFAULT_TARGET_DIR = "memory"
SCHEMA_VERSION = "2.0"


def run_cmd(args: List[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def parse_date(value: str | None) -> str:
    if not value:
        return dt.date.today().isoformat()
    return dt.date.fromisoformat(value).isoformat()


def pick_first(raw: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return default


def as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        text = value.replace("；", ";").replace("，", ",")
        parts = [x.strip() for x in text.replace(";", ",").split(",")]
        return [x for x in parts if x]
    return [str(value)]


def normalize_performance(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    out: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = pick_first(item, ["name", "指标", "metric"], "")
                val = pick_first(item, ["value", "数值", "val"], "")
                unit = pick_first(item, ["unit", "单位"], "")
                note = pick_first(item, ["note", "备注"], "")
                if str(name).strip():
                    out.append(
                        {
                            "name": str(name).strip(),
                            "value": val,
                            "unit": str(unit).strip(),
                            "note": str(note).strip(),
                        }
                    )
            else:
                out.append({"name": "metric", "value": item, "unit": "", "note": ""})
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append({"name": str(k), "value": v, "unit": "", "note": ""})
    return out


def normalize_watchlist(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    out: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                symbol = pick_first(item, ["symbol", "代码", "ticker"], "")
                name = pick_first(item, ["name", "名称"], "")
                thesis = pick_first(item, ["thesis", "逻辑", "观点"], "")
                trigger = pick_first(item, ["trigger", "触发条件"], "")
                risk = pick_first(item, ["risk", "风险"], "")
                if str(symbol).strip() or str(thesis).strip():
                    out.append(
                        {
                            "symbol": str(symbol).strip() or "N/A",
                            "name": str(name).strip(),
                            "thesis": str(thesis).strip() or "N/A",
                            "trigger": str(trigger).strip(),
                            "risk": str(risk).strip(),
                        }
                    )
            else:
                text = str(item).strip()
                if text:
                    out.append({"symbol": "N/A", "name": "", "thesis": text, "trigger": "", "risk": ""})
    return out


def normalize_next_day_plan(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    bias = str(pick_first(value, ["bias", "倾向", "预判"], "neutral")).strip().lower()
    if bias not in {"bullish", "neutral", "bearish"}:
        bias = "neutral"
    return {
        "bias": bias,
        "focus": as_string_list(pick_first(value, ["focus", "重点"], [])),
        "avoid": as_string_list(pick_first(value, ["avoid", "回避"], [])),
        "key_levels": as_string_list(pick_first(value, ["key_levels", "关键位"], [])),
        "notes": str(pick_first(value, ["notes", "备注"], "")).strip(),
    }


def normalize(raw: Dict[str, Any], forced_date: str | None = None) -> Dict[str, Any]:
    date_value = parse_date(forced_date or pick_first(raw, ["date", "日期"]))
    market = str(pick_first(raw, ["market", "市场"], DEFAULT_MARKET)).strip() or DEFAULT_MARKET
    summary = str(
        pick_first(raw, ["summary", "总结", "复盘", "content", "正文"], "No summary provided.")
    ).strip()

    themes = as_string_list(pick_first(raw, ["themes", "主题", "主线"], []))
    performance = normalize_performance(pick_first(raw, ["performance", "绩效", "指标"], []))
    watchlist = normalize_watchlist(pick_first(raw, ["watchlist", "观察池", "跟踪标的"], []))
    risks = as_string_list(pick_first(raw, ["risks", "风险"], []))
    actions = as_string_list(pick_first(raw, ["actions", "行动", "计划"], []))
    tags = as_string_list(pick_first(raw, ["tags", "标签"], []))
    next_day_plan = normalize_next_day_plan(pick_first(raw, ["next_day_plan", "次日计划"], {}))
    metrics = pick_first(raw, ["metrics", "市场指标"], {})
    meta = pick_first(raw, ["meta", "metadata"], {})

    if not isinstance(metrics, dict):
        metrics = {"raw": str(metrics)}
    if not isinstance(meta, dict):
        meta = {"raw": str(meta)}

    if not themes:
        themes = ["General"]
    if not actions:
        actions = ["Follow risk control plan."]

    return {
        "schema_version": SCHEMA_VERSION,
        "date": date_value,
        "market": market,
        "summary": summary,
        "themes": themes,
        "performance": performance,
        "watchlist": watchlist,
        "risks": risks,
        "actions": actions,
        "next_day_plan": next_day_plan,
        "metrics": metrics,
        "tags": tags,
        "meta": meta,
    }


def load_raw_input(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"summary": json.dumps(data, ensure_ascii=False)}
        return {"summary": str(data)}
    if suffix in {".txt", ".md"}:
        content = path.read_text(encoding="utf-8").strip()
        return {"summary": content}
    raise ValueError(f"Unsupported input file type: {suffix}")


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_json_if_exists(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def date_parts(date_value: str) -> Tuple[str, str, str]:
    d = dt.date.fromisoformat(date_value)
    year = d.strftime("%Y")
    ym = d.strftime("%Y-%m")
    ymd = d.strftime("%Y-%m-%d")
    return year, ym, ymd


def list_months_between(start: dt.date, end: dt.date) -> List[str]:
    months: List[str] = []
    cur = dt.date(start.year, start.month, 1)
    end_m = dt.date(end.year, end.month, 1)
    while cur <= end_m:
        months.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = dt.date(cur.year + 1, 1, 1)
        else:
            cur = dt.date(cur.year, cur.month + 1, 1)
    return months


def with_token_if_needed(repo_url: str, token: str | None) -> str:
    if not token:
        return repo_url
    parsed = urlsplit(repo_url)
    if parsed.scheme not in {"http", "https"}:
        return repo_url
    if "@" in parsed.netloc:
        return repo_url
    netloc = f"x-access-token:{token}@{parsed.netloc}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def ensure_repo(repo_dir: Path, repo_url: str, branch: str, skip_pull: bool = False) -> None:
    if (repo_dir / ".git").exists():
        run_cmd(["git", "remote", "set-url", "origin", repo_url], cwd=repo_dir)
        run_cmd(["git", "fetch", "origin"], cwd=repo_dir, check=False)

        checkout = run_cmd(["git", "checkout", branch], cwd=repo_dir, check=False)
        if checkout.returncode != 0:
            run_cmd(["git", "checkout", "-b", branch], cwd=repo_dir)

        if not skip_pull:
            pull = run_cmd(["git", "pull", "origin", branch], cwd=repo_dir, check=False)
            if pull.returncode != 0:
                stderr = (pull.stderr or "").lower()
                if "couldn't find remote ref" not in stderr and "no such ref was fetched" not in stderr:
                    raise RuntimeError(pull.stderr.strip() or "git pull failed")
        return

    ensure_dir(repo_dir.parent)
    clone = run_cmd(["git", "clone", "--branch", branch, repo_url, str(repo_dir)], check=False)
    if clone.returncode == 0:
        return

    stderr = (clone.stderr or "").lower()
    if "remote branch" in stderr and "not found" in stderr:
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        ensure_dir(repo_dir)
        run_cmd(["git", "init", "-b", branch], cwd=repo_dir)
        run_cmd(["git", "remote", "add", "origin", repo_url], cwd=repo_dir)
        return
    raise RuntimeError(clone.stderr.strip() or "git clone failed")


def repo_env() -> Tuple[str, str, Path, str, str | None, str, str]:
    repo_url = os.environ.get("MARKET_RECAP_GITHUB_URL", "").strip()
    if not repo_url:
        raise RuntimeError("MARKET_RECAP_GITHUB_URL is required.")

    branch = os.environ.get("MARKET_RECAP_BRANCH", "main").strip() or "main"
    repo_dir = Path(os.environ.get("MARKET_RECAP_REPO_DIR", "/tmp/market-memory-repo"))
    target_dir_name = os.environ.get("MARKET_RECAP_TARGET_DIR", DEFAULT_TARGET_DIR).strip() or DEFAULT_TARGET_DIR
    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    git_name = os.environ.get("GIT_AUTHOR_NAME", "market-memory-bot")
    git_email = os.environ.get("GIT_AUTHOR_EMAIL", "market-memory-bot@local")
    return repo_url, branch, repo_dir, target_dir_name, token, git_name, git_email


def short_summary(text: str, max_len: int = 80) -> str:
    t = " ".join(text.split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 3] + "..."


def build_manifest_entry(data: Dict[str, Any], snapshot_path: str, journal_offset: int) -> Dict[str, Any]:
    symbols = sorted({str(x.get("symbol", "")).strip() for x in data.get("watchlist", []) if str(x.get("symbol", "")).strip()})
    return {
        "date": data["date"],
        "market": data["market"],
        "themes": data.get("themes", []),
        "tags": data.get("tags", []),
        "symbols": symbols,
        "bias": data.get("next_day_plan", {}).get("bias", "neutral"),
        "summary_short": short_summary(str(data.get("summary", ""))),
        "snapshot_path": snapshot_path,
        "journal_offset": journal_offset,
        "updated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }


def update_month_manifest(memory_root: Path, ym: str, entry: Dict[str, Any]) -> None:
    manifest_path = memory_root / "indexes" / "monthly" / f"{ym}.json"
    manifest = load_json_if_exists(manifest_path, {"month": ym, "items": [], "updated_at": None})
    if not isinstance(manifest, dict):
        manifest = {"month": ym, "items": [], "updated_at": None}
    items = manifest.get("items")
    if not isinstance(items, list):
        items = []

    filtered = [it for it in items if isinstance(it, dict) and it.get("date") != entry["date"]]
    filtered.append(entry)
    filtered.sort(key=lambda x: x.get("date", ""), reverse=True)

    manifest["month"] = ym
    manifest["items"] = filtered
    manifest["updated_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    ensure_dir(manifest_path.parent)
    write_json(manifest_path, manifest)


def update_calendar_index(memory_root: Path, ym: str) -> None:
    calendar_path = memory_root / "indexes" / "calendar.json"
    calendar = load_json_if_exists(calendar_path, {"months": [], "updated_at": None})
    if not isinstance(calendar, dict):
        calendar = {"months": [], "updated_at": None}
    months = calendar.get("months")
    if not isinstance(months, list):
        months = []
    if ym not in months:
        months.append(ym)
        months.sort(reverse=True)
    calendar["months"] = months
    calendar["updated_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    write_json(calendar_path, calendar)


def update_latest(memory_root: Path, entry: Dict[str, Any]) -> None:
    latest_path = memory_root / "latest.json"
    latest = load_json_if_exists(latest_path, {})
    old_date = str(latest.get("date", "")) if isinstance(latest, dict) else ""
    if not old_date or entry["date"] >= old_date:
        write_json(latest_path, entry)


def ingest_memory(input_path: Path, forced_date: str | None, only_generate: bool, local_output_dir: Path) -> Path:
    raw = load_raw_input(input_path)
    data = normalize(raw, forced_date=forced_date)
    year, ym, ymd = date_parts(data["date"])

    if only_generate:
        snapshot_dir = local_output_dir / "daily" / year / ym
        ensure_dir(snapshot_dir)
        snapshot_path = snapshot_dir / f"{ymd}.json"
        write_json(snapshot_path, data)
        return snapshot_path

    repo_url, branch, repo_dir, target_dir_name, token, git_name, git_email = repo_env()
    effective_url = with_token_if_needed(repo_url, token)
    ensure_repo(repo_dir, effective_url, branch)
    run_cmd(["git", "config", "user.name", git_name], cwd=repo_dir)
    run_cmd(["git", "config", "user.email", git_email], cwd=repo_dir)

    memory_root = repo_dir / target_dir_name
    snapshot_dir = memory_root / "daily" / year / ym
    ensure_dir(snapshot_dir)
    snapshot_path = snapshot_dir / f"{ymd}.json"
    write_json(snapshot_path, data)

    journal_path = memory_root / "journal" / year / f"{ym}.jsonl"
    event = {
        "ts": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "op": "upsert",
        "date": data["date"],
        "market": data["market"],
        "snapshot_path": str(snapshot_path.relative_to(memory_root)),
        "digest": hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
    }
    append_jsonl(journal_path, event)

    journal_offset = 0
    with journal_path.open("r", encoding="utf-8") as f:
        for journal_offset, _ in enumerate(f, start=1):
            pass

    manifest_entry = build_manifest_entry(
        data=data,
        snapshot_path=str(snapshot_path.relative_to(memory_root)),
        journal_offset=journal_offset,
    )
    update_month_manifest(memory_root, ym, manifest_entry)
    update_calendar_index(memory_root, ym)
    update_latest(memory_root, manifest_entry)

    meta_dir = memory_root / "_meta"
    ensure_dir(meta_dir)
    write_json(
        meta_dir / "schema_version.json",
        {"schema_version": SCHEMA_VERSION, "updated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"},
    )

    run_cmd(["git", "add", target_dir_name], cwd=repo_dir)
    status = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip()
    if not status:
        print("No changes to commit.")
        return snapshot_path

    commit_msg = f"feat(memory): ingest {data['date']}"
    run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_dir)
    run_cmd(["git", "push", "origin", branch], cwd=repo_dir)
    return snapshot_path


def split_filter_values(raw_value: str | None) -> List[str]:
    if not raw_value:
        return []
    text = raw_value.replace("；", ",").replace(";", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def match_filter(entry: Dict[str, Any], key: str, required: List[str]) -> bool:
    if not required:
        return True
    values = entry.get(key, [])
    if not isinstance(values, list):
        return False
    vs = {str(x).lower() for x in values}
    return all(req.lower() in vs for req in required)


def build_recall_stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    theme_freq: Dict[str, int] = {}
    tag_freq: Dict[str, int] = {}
    symbol_freq: Dict[str, int] = {}
    bias_freq: Dict[str, int] = {"bullish": 0, "neutral": 0, "bearish": 0}

    for item in items:
        for t in item.get("themes", []):
            k = str(t)
            theme_freq[k] = theme_freq.get(k, 0) + 1
        for t in item.get("tags", []):
            k = str(t)
            tag_freq[k] = tag_freq.get(k, 0) + 1
        for w in item.get("watchlist", []):
            sym = str(w.get("symbol", "")).strip()
            if sym:
                symbol_freq[sym] = symbol_freq.get(sym, 0) + 1
        bias = str(item.get("next_day_plan", {}).get("bias", "neutral")).lower()
        if bias not in bias_freq:
            bias = "neutral"
        bias_freq[bias] += 1

    def top_n(freq: Dict[str, int], n: int = 10) -> List[Dict[str, Any]]:
        rows = [{"name": k, "count": v} for k, v in freq.items()]
        rows.sort(key=lambda x: (-x["count"], x["name"]))
        return rows[:n]

    return {
        "total_items": len(items),
        "bias_distribution": bias_freq,
        "top_themes": top_n(theme_freq),
        "top_tags": top_n(tag_freq),
        "top_symbols": top_n(symbol_freq),
    }


def render_recall_markdown(payload: Dict[str, Any]) -> str:
    q = payload.get("query", {})
    s = payload.get("stats", {})
    lines: List[str] = []
    lines.append(f"# Memory Recall - as_of {q.get('as_of', 'N/A')}")
    lines.append("")
    lines.append(f"- Lookback days: `{q.get('lookback_days', 'N/A')}`")
    lines.append(f"- Selected items: `{s.get('total_items', 0)}`")
    lines.append("")
    lines.append("## Bias Distribution")
    for k, v in s.get("bias_distribution", {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Top Themes")
    top_themes = s.get("top_themes", [])
    if top_themes:
        for row in top_themes:
            lines.append(f"- {row.get('name')}: {row.get('count')}")
    else:
        lines.append("- N/A")
    lines.append("")
    lines.append("## Recent Memory Items")
    for item in payload.get("items", []):
        lines.append(f"### {item.get('date')} [{item.get('market')}]")
        lines.append(f"- Themes: {', '.join(item.get('themes', [])) or 'N/A'}")
        lines.append(f"- Summary: {item.get('summary', '')}")
        lines.append(f"- Next bias: {item.get('next_day_plan', {}).get('bias', 'neutral')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def recall_memory(
    as_of: str,
    lookback_days: int,
    limit: int,
    market: str | None,
    required_themes: List[str],
    required_tags: List[str],
    required_symbols: List[str],
    output_path: Path,
    output_md: Path | None,
    skip_pull: bool,
) -> Dict[str, Any]:
    repo_url, branch, repo_dir, target_dir_name, token, _, _ = repo_env()
    effective_url = with_token_if_needed(repo_url, token)
    ensure_repo(repo_dir, effective_url, branch, skip_pull=skip_pull)

    memory_root = repo_dir / target_dir_name
    as_of_date = dt.date.fromisoformat(as_of)
    start_date = as_of_date - dt.timedelta(days=max(lookback_days - 1, 0))

    months = list_months_between(start_date, as_of_date)
    manifest_entries: List[Dict[str, Any]] = []
    for ym in months:
        manifest_path = memory_root / "indexes" / "monthly" / f"{ym}.json"
        manifest = load_json_if_exists(manifest_path, {"items": []})
        items = manifest.get("items", []) if isinstance(manifest, dict) else []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    manifest_entries.append(it)

    filtered_entries: List[Dict[str, Any]] = []
    for entry in manifest_entries:
        date_str = str(entry.get("date", ""))
        try:
            d = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        if d < start_date or d > as_of_date:
            continue
        if market and str(entry.get("market", "")).lower() != market.lower():
            continue
        if not match_filter(entry, "themes", required_themes):
            continue
        if not match_filter(entry, "tags", required_tags):
            continue
        if not match_filter(entry, "symbols", required_symbols):
            continue
        filtered_entries.append(entry)

    filtered_entries.sort(key=lambda x: x.get("date", ""), reverse=True)
    selected_entries = filtered_entries[: max(limit, 0)]

    items: List[Dict[str, Any]] = []
    for entry in selected_entries:
        rel = str(entry.get("snapshot_path", "")).strip()
        if not rel:
            continue
        snapshot_path = memory_root / rel
        if not snapshot_path.exists():
            continue
        snapshot = load_json_if_exists(snapshot_path, {})
        if isinstance(snapshot, dict):
            snapshot["_snapshot_path"] = rel
            items.append(snapshot)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "query": {
            "as_of": as_of,
            "lookback_days": lookback_days,
            "limit": limit,
            "market": market,
            "themes": required_themes,
            "tags": required_tags,
            "symbols": required_symbols,
        },
        "stats": build_recall_stats(items),
        "items": items,
    }

    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    if output_md is not None:
        ensure_dir(output_md.parent)
        output_md.write_text(render_recall_markdown(payload), encoding="utf-8")

    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub-backed memory sync for market recaps.")
    sub = parser.add_subparsers(dest="mode", required=True)

    ingest = sub.add_parser("ingest", help="Ingest post-market recap into GitHub memory")
    ingest.add_argument("--input", required=True, help="Input file (.json/.txt/.md)")
    ingest.add_argument("--date", help="Override date in YYYY-MM-DD")
    ingest.add_argument("--only-generate", action="store_true", help="Only generate local snapshot without git sync")
    ingest.add_argument("--output-dir", default="output-memory", help="Local output root for --only-generate")

    recall = sub.add_parser("recall", help="Recall memory window for pre-market planning")
    recall.add_argument("--as-of", default=dt.date.today().isoformat(), help="As-of date in YYYY-MM-DD")
    recall.add_argument("--lookback-days", type=int, default=60, help="Lookback window in calendar days")
    recall.add_argument("--limit", type=int, default=30, help="Max recalled memory items")
    recall.add_argument("--market", help="Filter by market, e.g. CN-A")
    recall.add_argument("--themes", help="Comma-separated required themes")
    recall.add_argument("--tags", help="Comma-separated required tags")
    recall.add_argument("--symbols", help="Comma-separated required symbols")
    recall.add_argument("--output", default="output-memory/recall.json", help="Recall JSON output path")
    recall.add_argument("--output-md", default="output-memory/recall.md", help="Optional recall markdown output path")
    recall.add_argument("--no-markdown", action="store_true", help="Disable markdown output")
    recall.add_argument("--skip-pull", action="store_true", help="Skip git pull when reading memory")

    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()

        if args.mode == "ingest":
            input_path = Path(args.input).expanduser().resolve()
            output_dir = Path(args.output_dir).expanduser().resolve()
            ensure_dir(output_dir)
            snapshot_path = ingest_memory(
                input_path=input_path,
                forced_date=args.date,
                only_generate=args.only_generate,
                local_output_dir=output_dir,
            )
            print(f"Ingested snapshot: {snapshot_path}")
            return 0

        if args.mode == "recall":
            output_path = Path(args.output).expanduser().resolve()
            output_md = None if args.no_markdown else Path(args.output_md).expanduser().resolve()
            payload = recall_memory(
                as_of=parse_date(args.as_of),
                lookback_days=max(args.lookback_days, 1),
                limit=max(args.limit, 1),
                market=args.market,
                required_themes=split_filter_values(args.themes),
                required_tags=split_filter_values(args.tags),
                required_symbols=split_filter_values(args.symbols),
                output_path=output_path,
                output_md=output_md,
                skip_pull=args.skip_pull,
            )
            print(
                "Recall generated: "
                f"{output_path} (items={payload.get('stats', {}).get('total_items', 0)})"
            )
            return 0

        raise RuntimeError(f"Unsupported mode: {args.mode}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
