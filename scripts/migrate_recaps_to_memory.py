#!/usr/bin/env python3
"""Migrate legacy recaps/*/recap.json into the memory/ storage layout.

This script is designed for one-time or incremental backfill.
It batches writes and creates a single commit for better efficiency.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from memory_sync import (
    SCHEMA_VERSION,
    append_jsonl,
    build_manifest_entry,
    date_parts,
    ensure_dir,
    ensure_repo,
    load_json_if_exists,
    normalize,
    run_cmd,
    with_token_if_needed,
    write_json,
)


def parse_date_or_none(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def iter_legacy_recap_files(source_root: Path, recaps_dir: str) -> List[Path]:
    base = source_root / recaps_dir
    if not base.exists():
        base = source_root
    files = sorted(base.rglob("recap.json"))
    return [p for p in files if p.is_file()]


def load_legacy_recap(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Legacy recap must be a JSON object: {path}")
    return data


def in_date_window(date_value: str, start: dt.date | None, end: dt.date | None) -> bool:
    d = dt.date.fromisoformat(date_value)
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def digest_payload(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def split_filters(raw: str | None) -> List[str]:
    if not raw:
        return []
    text = raw.replace("；", ",").replace(";", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def pass_contains_filter(values: Iterable[str], required: List[str]) -> bool:
    if not required:
        return True
    got = {str(v).lower() for v in values}
    return all(x.lower() in got for x in required)


def upsert_manifest_item(manifest: Dict[str, Any], entry: Dict[str, Any]) -> None:
    items = manifest.get("items")
    if not isinstance(items, list):
        items = []

    filtered: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("date") == entry.get("date"):
            continue
        filtered.append(it)

    filtered.append(entry)
    filtered.sort(key=lambda x: x.get("date", ""), reverse=True)
    manifest["items"] = filtered
    manifest["updated_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy recaps into memory format")
    parser.add_argument("--source-root", required=True, help="Legacy repo/local root path")
    parser.add_argument("--recaps-dir", default="recaps", help="Legacy recaps directory name")
    parser.add_argument("--from-date", help="Start date (inclusive), YYYY-MM-DD")
    parser.add_argument("--to-date", help="End date (inclusive), YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Max files to migrate after filtering")
    parser.add_argument("--market", help="Only migrate specified market, e.g. CN-A")
    parser.add_argument("--themes", help="Require themes (comma-separated)")
    parser.add_argument("--tags", help="Require tags (comma-separated)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing same-date snapshots")
    parser.add_argument("--dry-run", action="store_true", help="Preview migration plan without writing")
    parser.add_argument("--no-push", action="store_true", help="Commit locally but do not push")

    parser.add_argument("--repo-url", help="Override target GitHub repo URL")
    parser.add_argument("--branch", help="Override target branch")
    parser.add_argument("--repo-dir", help="Override local repo cache dir")
    parser.add_argument("--target-dir", help="Override target memory directory in repo")
    parser.add_argument("--git-name", help="Override git author name")
    parser.add_argument("--git-email", help="Override git author email")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        source_root = Path(args.source_root).expanduser().resolve()
        if not source_root.exists():
            raise FileNotFoundError(f"source root not found: {source_root}")

        start_date = parse_date_or_none(args.from_date)
        end_date = parse_date_or_none(args.to_date)
        if start_date and end_date and start_date > end_date:
            raise ValueError("from-date cannot be later than to-date")

        required_themes = split_filters(args.themes)
        required_tags = split_filters(args.tags)

        legacy_files = iter_legacy_recap_files(source_root, args.recaps_dir)
        if not legacy_files:
            raise RuntimeError("no legacy recap.json files found")

        candidates: List[Tuple[Path, Dict[str, Any]]] = []
        for legacy_path in legacy_files:
            raw = load_legacy_recap(legacy_path)
            normalized = normalize(raw, forced_date=None)

            if not in_date_window(normalized["date"], start_date, end_date):
                continue
            if args.market and str(normalized.get("market", "")).lower() != args.market.lower():
                continue
            if not pass_contains_filter(normalized.get("themes", []), required_themes):
                continue
            if not pass_contains_filter(normalized.get("tags", []), required_tags):
                continue

            candidates.append((legacy_path, normalized))

        candidates.sort(key=lambda x: x[1]["date"])
        if args.limit is not None and args.limit >= 0:
            candidates = candidates[: args.limit]

        if not candidates:
            print("No records matched filters. Nothing to migrate.")
            return 0

        repo_url = (args.repo_url or os.environ.get("MARKET_RECAP_GITHUB_URL", "")).strip()
        if not repo_url and not args.dry_run:
            raise RuntimeError("MARKET_RECAP_GITHUB_URL (or --repo-url) is required when not dry-run")

        branch = (args.branch or os.environ.get("MARKET_RECAP_BRANCH", "main")).strip() or "main"
        repo_dir = Path(args.repo_dir or os.environ.get("MARKET_RECAP_REPO_DIR", "/tmp/market-memory-repo"))
        target_dir_name = (args.target_dir or os.environ.get("MARKET_RECAP_TARGET_DIR", "memory")).strip() or "memory"
        token = os.environ.get("GITHUB_TOKEN", "").strip() or None
        git_name = args.git_name or os.environ.get("GIT_AUTHOR_NAME", "market-memory-bot")
        git_email = args.git_email or os.environ.get("GIT_AUTHOR_EMAIL", "market-memory-bot@local")

        print(f"Candidates: {len(candidates)}")
        print(f"Date range: {candidates[0][1]['date']} -> {candidates[-1][1]['date']}")

        if args.dry_run:
            for legacy_path, normalized in candidates[:10]:
                print(f"DRY-RUN {normalized['date']} {normalized['market']} <- {legacy_path}")
            if len(candidates) > 10:
                print(f"... and {len(candidates) - 10} more")
            return 0

        effective_url = with_token_if_needed(repo_url, token)
        ensure_repo(repo_dir, effective_url, branch)
        run_cmd(["git", "config", "user.name", git_name], cwd=repo_dir)
        run_cmd(["git", "config", "user.email", git_email], cwd=repo_dir)

        memory_root = repo_dir / target_dir_name
        ensure_dir(memory_root)

        calendar_path = memory_root / "indexes" / "calendar.json"
        calendar = load_json_if_exists(calendar_path, {"months": [], "updated_at": None})
        months = calendar.get("months") if isinstance(calendar, dict) else []
        month_set = set(months if isinstance(months, list) else [])

        latest_path = memory_root / "latest.json"
        latest = load_json_if_exists(latest_path, {})
        latest_date = str(latest.get("date", "")) if isinstance(latest, dict) else ""

        manifest_cache: Dict[str, Dict[str, Any]] = {}
        journal_counts: Dict[str, int] = {}
        touched_months: set[str] = set()

        migrated = 0
        skipped_existing = 0
        touched_days: List[str] = []

        for _, data in candidates:
            year, ym, ymd = date_parts(data["date"])
            snapshot_rel = Path("daily") / year / ym / f"{ymd}.json"
            snapshot_path = memory_root / snapshot_rel
            ensure_dir(snapshot_path.parent)

            if snapshot_path.exists() and not args.overwrite:
                skipped_existing += 1
                continue

            write_json(snapshot_path, data)

            journal_path = memory_root / "journal" / year / f"{ym}.jsonl"
            event = {
                "ts": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                "op": "upsert",
                "date": data["date"],
                "market": data["market"],
                "snapshot_path": str(snapshot_rel),
                "digest": digest_payload(data),
                "schema_version": SCHEMA_VERSION,
                "source": "legacy-recap-migration",
            }
            append_jsonl(journal_path, event)

            if ym not in journal_counts:
                journal_counts[ym] = count_jsonl_lines(journal_path)
            else:
                journal_counts[ym] += 1

            manifest_path = memory_root / "indexes" / "monthly" / f"{ym}.json"
            manifest = manifest_cache.get(ym)
            if manifest is None:
                manifest = load_json_if_exists(manifest_path, {"month": ym, "items": [], "updated_at": None})
                if not isinstance(manifest, dict):
                    manifest = {"month": ym, "items": [], "updated_at": None}
                manifest_cache[ym] = manifest

            entry = build_manifest_entry(data, str(snapshot_rel), journal_counts[ym])
            upsert_manifest_item(manifest, entry)

            month_set.add(ym)
            touched_months.add(ym)
            touched_days.append(data["date"])
            migrated += 1

            if not latest_date or data["date"] >= latest_date:
                latest = entry
                latest_date = data["date"]

        if migrated == 0:
            print(f"No new records migrated. skipped_existing={skipped_existing}")
            return 0

        for ym in touched_months:
            manifest_path = memory_root / "indexes" / "monthly" / f"{ym}.json"
            ensure_dir(manifest_path.parent)
            write_json(manifest_path, manifest_cache[ym])

        calendar_payload = {
            "months": sorted(month_set, reverse=True),
            "updated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }
        write_json(calendar_path, calendar_payload)
        write_json(latest_path, latest)

        meta_path = memory_root / "_meta" / "schema_version.json"
        ensure_dir(meta_path.parent)
        write_json(
            meta_path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                "migrated_records": migrated,
            },
        )

        run_cmd(["git", "add", target_dir_name], cwd=repo_dir)
        status = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip()
        if not status:
            print("No changes to commit.")
            return 0

        commit_msg = (
            "feat(memory): migrate legacy recaps "
            f"{touched_days[0]}..{touched_days[-1]} ({migrated} records)"
        )
        run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_dir)
        if not args.no_push:
            run_cmd(["git", "push", "origin", branch], cwd=repo_dir)

        print(
            "Migration completed: "
            f"migrated={migrated}, skipped_existing={skipped_existing}, months={len(touched_months)}"
        )
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
