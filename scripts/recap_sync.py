#!/usr/bin/env python3
"""
Market recap sync tool.

1) Normalize raw recap data into canonical JSON
2) Render markdown + CSV artifacts
3) Sync artifacts to a GitHub repo and push
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit, urlunsplit


DEFAULT_MARKET = "CN-A"


def run_cmd(
    args: List[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize recap data and sync to GitHub.")
    parser.add_argument("--input", required=True, help="Input file (.json/.txt/.md)")
    parser.add_argument("--output-dir", default="output", help="Local build output root directory")
    parser.add_argument("--date", help="Override date in YYYY-MM-DD")
    parser.add_argument(
        "--only-generate",
        action="store_true",
        help="Only generate local artifacts without git sync",
    )
    return parser.parse_args()


def parse_date(value: str | None) -> str:
    if not value:
        return dt.date.today().isoformat()
    return dt.date.fromisoformat(value).isoformat()


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
        parts = [x.strip() for x in value.replace("；", ";").replace("，", ",").split(",")]
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
                    out.append(
                        {
                            "symbol": "N/A",
                            "name": "",
                            "thesis": text,
                            "trigger": "",
                            "risk": "",
                        }
                    )
    return out


def normalize(raw: Dict[str, Any], forced_date: str | None = None) -> Dict[str, Any]:
    date_value = parse_date(forced_date or pick_first(raw, ["date", "日期"]))
    market = str(pick_first(raw, ["market", "市场"], DEFAULT_MARKET))
    summary = str(
        pick_first(raw, ["summary", "总结", "复盘", "content", "正文"], "No summary provided.")
    ).strip()

    themes = as_string_list(pick_first(raw, ["themes", "主题", "主线"], []))
    performance = normalize_performance(pick_first(raw, ["performance", "绩效", "指标"], []))
    watchlist = normalize_watchlist(pick_first(raw, ["watchlist", "观察池", "跟踪标的"], []))
    risks = as_string_list(pick_first(raw, ["risks", "风险"], []))
    actions = as_string_list(pick_first(raw, ["actions", "行动", "计划"], []))
    tags = as_string_list(pick_first(raw, ["tags", "标签"], []))
    meta = pick_first(raw, ["meta", "metadata"], {})
    if not isinstance(meta, dict):
        meta = {"raw_meta": str(meta)}

    if not themes:
        themes = ["General"]
    if not actions:
        actions = ["Follow risk control plan."]

    return {
        "date": date_value,
        "market": market,
        "summary": summary,
        "themes": themes,
        "performance": performance,
        "watchlist": watchlist,
        "risks": risks,
        "actions": actions,
        "tags": tags,
        "meta": meta,
    }


def date_paths(date_value: str) -> Tuple[str, str, str]:
    d = dt.date.fromisoformat(date_value)
    year = d.strftime("%Y")
    ym = d.strftime("%Y-%m")
    ymd = d.strftime("%Y-%m-%d")
    return year, ym, ymd


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def render_markdown(data: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Daily Market Recap - {data['date']}")
    lines.append("")
    lines.append(f"- Market: `{data['market']}`")
    lines.append(f"- Tags: `{', '.join(data['tags']) if data['tags'] else 'N/A'}`")
    lines.append("")
    lines.append("## Summary")
    lines.append(data["summary"])
    lines.append("")
    lines.append("## Themes")
    for t in data["themes"]:
        lines.append(f"- {t}")
    lines.append("")
    lines.append("## Performance")
    if data["performance"]:
        for row in data["performance"]:
            name = row.get("name", "")
            value = row.get("value", "")
            unit = row.get("unit", "")
            note = row.get("note", "")
            tail = f" ({note})" if note else ""
            lines.append(f"- {name}: {value}{unit}{tail}")
    else:
        lines.append("- N/A")
    lines.append("")
    lines.append("## Watchlist")
    if data["watchlist"]:
        for row in data["watchlist"]:
            base = (
                f"{row.get('symbol', '')} {row.get('name', '')}".strip()
                + f": {row.get('thesis', '')}"
            )
            details: List[str] = []
            if row.get("trigger"):
                details.append(f"Trigger={row['trigger']}")
            if row.get("risk"):
                details.append(f"Risk={row['risk']}")
            detail_suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(f"- {base}{detail_suffix}")
    else:
        lines.append("- N/A")
    lines.append("")
    lines.append("## Risks")
    if data["risks"]:
        for r in data["risks"]:
            lines.append(f"- {r}")
    else:
        lines.append("- N/A")
    lines.append("")
    lines.append("## Actions")
    for a in data["actions"]:
        lines.append(f"- {a}")
    lines.append("")
    return "\n".join(lines)


def build_artifacts(data: Dict[str, Any], output_root: Path) -> Path:
    year, ym, ymd = date_paths(data["date"])
    target_dir = output_root / year / ym / ymd
    ensure_dir(target_dir)

    write_json(target_dir / "recap.json", data)
    write_csv(
        target_dir / "performance.csv",
        data["performance"],
        ["name", "value", "unit", "note"],
    )
    write_csv(
        target_dir / "watchlist.csv",
        data["watchlist"],
        ["symbol", "name", "thesis", "trigger", "risk"],
    )
    (target_dir / "recap.md").write_text(render_markdown(data), encoding="utf-8")
    return target_dir


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


def ensure_repo(repo_dir: Path, repo_url: str, branch: str) -> None:
    if (repo_dir / ".git").exists():
        run_cmd(["git", "remote", "set-url", "origin", repo_url], cwd=repo_dir)
        run_cmd(["git", "fetch", "origin"], cwd=repo_dir, check=False)

        checkout = run_cmd(["git", "checkout", branch], cwd=repo_dir, check=False)
        if checkout.returncode != 0:
            run_cmd(["git", "checkout", "-b", branch], cwd=repo_dir)

        pull = run_cmd(["git", "pull", "origin", branch], cwd=repo_dir, check=False)
        if pull.returncode != 0:
            stderr = (pull.stderr or "").lower()
            if "couldn't find remote ref" not in stderr and "no such ref was fetched" not in stderr:
                raise RuntimeError(pull.stderr.strip() or "git pull failed")
        return

    ensure_dir(repo_dir.parent)
    clone = run_cmd(
        ["git", "clone", "--branch", branch, repo_url, str(repo_dir)],
        check=False,
    )
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


def load_json_if_exists(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def update_indexes(target_root: Path, data: Dict[str, Any], relative_daily_dir: str) -> None:
    index_path = target_root / "_index.json"
    latest_path = target_root / "latest.json"

    index_data = load_json_if_exists(index_path, {"items": []})
    if not isinstance(index_data, dict):
        index_data = {"items": []}
    items = index_data.get("items")
    if not isinstance(items, list):
        items = []

    entry = {
        "date": data["date"],
        "market": data["market"],
        "summary": data["summary"],
        "themes": data["themes"],
        "path": relative_daily_dir,
        "updated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }

    filtered = [x for x in items if isinstance(x, dict) and x.get("date") != data["date"]]
    filtered.append(entry)
    filtered.sort(key=lambda x: x.get("date", ""), reverse=True)
    index_data["items"] = filtered

    write_json(index_path, index_data)
    write_json(latest_path, entry)


def sync_to_repo(artifact_dir: Path, data: Dict[str, Any]) -> None:
    repo_url = os.environ.get("MARKET_RECAP_GITHUB_URL", "").strip()
    if not repo_url:
        raise RuntimeError("MARKET_RECAP_GITHUB_URL is required for sync.")

    branch = os.environ.get("MARKET_RECAP_BRANCH", "main").strip() or "main"
    repo_dir = Path(os.environ.get("MARKET_RECAP_REPO_DIR", "/tmp/market-recap-sync-repo"))
    target_dir_name = os.environ.get("MARKET_RECAP_TARGET_DIR", "recaps").strip() or "recaps"
    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    git_name = os.environ.get("GIT_AUTHOR_NAME", "market-recap-bot")
    git_email = os.environ.get("GIT_AUTHOR_EMAIL", "market-recap-bot@local")

    effective_url = with_token_if_needed(repo_url, token)
    ensure_repo(repo_dir, effective_url, branch)

    run_cmd(["git", "config", "user.name", git_name], cwd=repo_dir)
    run_cmd(["git", "config", "user.email", git_email], cwd=repo_dir)

    year, ym, ymd = date_paths(data["date"])
    relative_daily_dir = str(Path(year) / ym / ymd)

    dest_root = repo_dir / target_dir_name
    dest_daily_dir = dest_root / relative_daily_dir
    ensure_dir(dest_daily_dir.parent)
    if dest_daily_dir.exists():
        shutil.rmtree(dest_daily_dir)
    shutil.copytree(artifact_dir, dest_daily_dir)

    update_indexes(dest_root, data, relative_daily_dir)

    run_cmd(["git", "add", target_dir_name], cwd=repo_dir)
    status = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip()
    if not status:
        print("No changes to commit.")
        return

    commit_msg = f"chore(recap): sync {data['date']} recap"
    run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_dir)
    run_cmd(["git", "push", "origin", branch], cwd=repo_dir)
    print(f"Synced and pushed to {repo_url} ({branch}).")


def main() -> int:
    try:
        args = parse_args()
        input_path = Path(args.input).expanduser().resolve()
        output_root = Path(args.output_dir).expanduser().resolve()
        ensure_dir(output_root)

        raw = load_raw_input(input_path)
        normalized = normalize(raw, forced_date=args.date)
        artifact_dir = build_artifacts(normalized, output_root)
        print(f"Artifacts generated at: {artifact_dir}")

        if args.only_generate:
            return 0

        sync_to_repo(artifact_dir, normalized)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
