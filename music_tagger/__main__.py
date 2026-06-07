#!/usr/bin/env python3
"""music_tagger — album-clustered metadata tagger.

Pipeline:
  1. Cluster files by folder (= album).
  2. beets MusicBrainz autotagger proposes a release per album (cached in SQLite).
  3. strong match  -> auto-accept top candidate.
     low/medium    -> Claude selects among candidates (cached in SQLite).
     none / error  -> flagged, no proposal.
  4. Dry-run by default: writes a current-vs-proposed report, no file changes.
     --apply writes tags and journals originals for `--undo <run-id>`.

Usage:
  python -m music_tagger <dir>                 # dry run + report
  python -m music_tagger <dir> --limit 5       # first 5 albums
  python -m music_tagger <dir> --apply         # write tags (journals for undo)
  python -m music_tagger --list-runs
  python -m music_tagger --undo <run-id>
"""
import os
import re
import sys
import io
import time
import logging
import argparse
import atexit
import signal
from pathlib import Path
from datetime import datetime

from .tags import read_existing_tags, write_tags, restore_tags, diff_tags
from .store import Store
from . import matcher
from . import verify
from . import report

# ── keys ────────────────────────────────────────────────────────────────────
_KEYS_FILES = [Path(".env/keys.txt"), Path(".venv/keys.txt"), Path("keys.txt")]


def _load_keys() -> None:
    for path in _KEYS_FILES:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sep = "=" if "=" in line else ":" if ":" in line else None
            if not sep:
                continue
            name, value = line.split(sep, 1)
            name, value = name.strip(), value.strip()
            if name in ("ANTHROPIC_API_KEY", "anthropicKey", "anthropic_api_key") and value:
                os.environ.setdefault("ANTHROPIC_API_KEY", value)
        return


_load_keys()

DB_DEFAULT = "music_tagger.db"
LOCKFILE = Path(__file__).resolve().parent.parent / ".music_tagger.lock"

# ── logging ───────────────────────────────────────────────────────────────────
_log_path = Path("music_tagger.log")
if _log_path.exists() and _log_path.stat().st_size > 0:
    try:
        bak = _log_path.with_suffix(".log.bak")
        bak.unlink(missing_ok=True)
        _log_path.rename(bak)
    except OSError:
        pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                               errors="replace", line_buffering=True)),
    ],
)
log = logging.getLogger("music_tagger")


# ── lockfile ──────────────────────────────────────────────────────────────────
# Age-based, not PID-based: os.kill(pid, 0) is unsafe on Windows (signal 0 maps to
# CTRL_C_EVENT and can disturb the target). A lock older than this is treated as
# a crashed run and reclaimed.
_LOCK_STALE_SECONDS = 3600


def _acquire_lock() -> None:
    if LOCKFILE.exists():
        try:
            age = time.time() - LOCKFILE.stat().st_mtime
            owner = LOCKFILE.read_text().strip()
        except OSError:
            age, owner = _LOCK_STALE_SECONDS + 1, "?"
        if age < _LOCK_STALE_SECONDS:
            raise RuntimeError(
                f"Another run may be active ({owner}, lock age {int(age)}s). "
                f"If it crashed, delete {LOCKFILE} and retry."
            )
        LOCKFILE.unlink(missing_ok=True)
    fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
    atexit.register(lambda: LOCKFILE.unlink(missing_ok=True))
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: (LOCKFILE.unlink(missing_ok=True), sys.exit(1)))
        except Exception:
            pass


# ── per-album processing ────────────────────────────────────────────────────
def _decide(proposal: dict, folder: str, store: Store, client, model: str,
            use_cache: bool, eval_only: bool) -> dict:
    """Map a proposal -> {action, chosen_index, confidence, reasoning, from_cache.claude}."""
    rec = proposal["recommendation"]
    cands = proposal["candidates"]
    out = {"chosen_index": None, "confidence": None, "reasoning": "", "claude_cached": False}

    if rec == "error":
        out.update(action="error", reasoning=proposal.get("error", "match error"))
    elif rec == "none" or not cands:
        out.update(action="unresolved", reasoning="No MusicBrainz candidates")
    elif rec == "strong":
        out.update(action="auto", chosen_index=0, confidence=100,
                   reasoning="Strong autotag match")
    elif eval_only:
        out.update(action="unresolved", reasoning=f"{rec} match — Claude skipped (--eval-only)")
    else:
        chash = matcher.candidates_hash(proposal)
        verdict = store.get_claude(proposal["_album_key"], chash) if use_cache else None
        if verdict is not None:
            out["claude_cached"] = True
        else:
            verdict = verify.verify_album(folder, proposal["current"], cands, client, model)
            store.put_claude(proposal["_album_key"], chash, verdict)
        idx = verdict["chosen_index"]
        out.update(
            chosen_index=idx,
            confidence=verdict["confidence"],
            reasoning=verdict["reasoning"],
            action="claude_selected" if idx is not None else "claude_rejected",
        )
    return out


def _build_result(folder: str, key: str, proposal: dict, decision: dict,
                  lookup_cached: bool) -> dict:
    cands = proposal["candidates"]
    idx = decision["chosen_index"]
    chosen = cands[idx] if idx is not None else None

    files = []
    n_changed = 0
    for path, cur in proposal["current"].items():
        proposed = chosen["per_file"].get(path, {}) if chosen else {}
        changed = diff_tags(cur, proposed) if proposed else []
        if changed:
            n_changed += 1
        files.append({
            "path": path, "name": Path(path).name,
            "current": cur, "proposed": proposed, "changed": changed,
        })

    chosen_summary = None
    if chosen:
        chosen_summary = {
            "album": chosen["album"], "albumartist": chosen["albumartist"],
            "year": chosen["year"], "album_id": chosen["album_id"],
            "distance": chosen["distance"],
        }

    return {
        "folder": folder, "album_key": key,
        "recommendation": proposal["recommendation"], "action": decision["action"],
        "from_cache": {"lookup": lookup_cached, "claude": decision["claude_cached"]},
        "cur_artist": proposal["cur_artist"], "cur_album": proposal["cur_album"],
        "chosen": chosen_summary, "confidence": decision["confidence"],
        "reasoning": decision["reasoning"],
        "unreadable": proposal.get("unreadable", []),
        "files": files, "n_files": len(files), "n_changed_files": n_changed,
    }


def run(directory: str, *, apply: bool, limit: int | None, model: str, db_path: str,
        confidence: int, use_cache: bool, eval_only: bool) -> None:
    matcher.init_beets()
    store = Store(db_path)
    client = None  # created lazily on first Claude call
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    albums = matcher.cluster_albums(directory)
    if not albums:
        log.warning(f"No MP3/FLAC files found under {directory}")
        return
    folders = list(albums.items())
    if limit:
        folders = folders[:limit]

    log.info(f"{len(albums)} album folder(s) found; processing {len(folders)}"
             + (f" (--limit {limit})" if limit else ""))
    log.info("DRY RUN — no files will be modified" if not apply else f"APPLY — run_id={run_id}")
    log.info("─" * 64)

    results = []
    claude_albums = 0
    for i, (folder, files) in enumerate(folders, 1):
        key = matcher.album_key(files)
        log.info(f"[{i}/{len(folders)}] {Path(folder).name}  ({len(files)} files)")

        proposal = store.get_lookup(key) if use_cache else None
        lookup_cached = proposal is not None
        if proposal is None:
            proposal = matcher.match_album(files)
            # Don't cache transient errors — let them retry next run.
            if proposal["recommendation"] != "error":
                store.put_lookup(key, str(folder), proposal["recommendation"], proposal)
        proposal["_album_key"] = key
        if proposal.get("unreadable"):
            log.info(f"    skipped {len(proposal['unreadable'])} unreadable file(s)")

        if client is None and not eval_only and proposal["recommendation"] in ("low", "medium"):
            import anthropic
            client = anthropic.Anthropic()

        decision = _decide(proposal, str(folder), store, client, model, use_cache, eval_only)
        if decision["action"] in ("claude_selected", "claude_rejected"):
            claude_albums += 1
        log.info(f"    {proposal['recommendation']:<7} -> {decision['action']}"
                 + (f"  (conf {decision['confidence']})" if decision["confidence"] is not None else ""))

        result = _build_result(str(folder), key, proposal, decision, lookup_cached)

        if apply and result["chosen"] and (decision["confidence"] or 0) >= confidence:
            _apply_writes(result, store, run_id, confidence)

        results.append(result)

    _summarize(results, run_id, apply)
    report.save_reports(results, verify.estimate_cost(claude_albums, model), model, dry_run=not apply)
    store.close()


def _apply_writes(result: dict, store: Store, run_id: str, confidence: int) -> None:
    for f in result["files"]:
        if not f["changed"]:
            continue
        original = read_existing_tags(f["path"])  # fresh snapshot for the journal
        if write_tags(f["path"], f["proposed"]):
            store.log_write(run_id, f["path"], original, f["proposed"])
            log.info(f"      ✓ wrote {f['name']} ({', '.join(f['changed'])})")
        else:
            log.error(f"      ✗ failed to write {f['name']}")


def _summarize(results: list[dict], run_id: str, apply: bool) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    n_changed = sum(r["n_changed_files"] for r in results)
    log.info("═" * 64)
    log.info(f"SUMMARY  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    for action in ("auto", "claude_selected", "claude_rejected", "unresolved", "error"):
        if counts.get(action):
            log.info(f"  {action:<16}: {counts[action]} album(s)")
    log.info(f"  files with changes: {n_changed}")
    if apply:
        log.info(f"  run_id (for --undo): {run_id}")
    log.info("═" * 64)


# ── rollback ──────────────────────────────────────────────────────────────────
def cmd_list_runs(db_path: str) -> None:
    store = Store(db_path)
    runs = store.list_runs()
    if not runs:
        print("No write runs recorded.")
    for r in runs:
        when = datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {r['run_id']}  {when}  {r['files']} file(s) written")
    store.close()


def cmd_undo(db_path: str, run_id: str) -> None:
    store = Store(db_path)
    writes = store.get_run_writes(run_id)
    if not writes:
        print(f"No writes found for run {run_id}.")
        store.close()
        return
    log.info(f"Restoring {len(writes)} file(s) from run {run_id}...")
    restored = 0
    for w in writes:
        if restore_tags(w["file_path"], w["original"]):
            restored += 1
            log.info(f"  ✓ restored {Path(w['file_path']).name}")
        else:
            log.error(f"  ✗ failed to restore {w['file_path']}")
    log.info(f"Restored {restored}/{len(writes)} file(s).")
    store.close()


# ── corruption scan ───────────────────────────────────────────────────────────
def _short_reason(e) -> str:
    s = str(e)
    if "is not a valid FLAC file" in s:
        return "invalid FLAC (corrupt)"
    if "read 0 bytes" in s:
        return "empty / zero-byte read"
    m = re.search(r"said (\d+) bytes, read (\d+) bytes", s)
    if m:
        return f"truncated ({m.group(2)}/{m.group(1)} bytes)"
    if "not a valid" in s or "Errno 2" in s:
        return "missing/invalid"
    return s[-90:]


def cmd_list_unreadable(directory: str) -> None:
    """Read-only scan: attempt to read every audio file, list the ones that fail."""
    albums = matcher.cluster_albums(directory)
    total = sum(len(f) for f in albums.values())
    log.info(f"Scanning {total} files across {len(albums)} album(s) for unreadable files...")

    bad_by_album: dict[str, list[tuple[str, str]]] = {}
    n_bad = 0
    for folder, files in albums.items():
        for p in files:
            try:
                matcher._read_item(p, attempts=2, delay=0.3)
            except Exception as e:  # noqa: BLE001
                bad_by_album.setdefault(str(folder), []).append((p.name, _short_reason(e)))
                n_bad += 1

    out = Path("music_tagger_unreadable.txt")
    lines = [
        f"# MusicTagger unreadable-file scan — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# {n_bad} unreadable of {total} files across {len(bad_by_album)} album(s)",
        "",
    ]
    for folder in sorted(bad_by_album):
        items = bad_by_album[folder]
        lines.append(f"[{folder}]  {len(items)}/{len(albums[Path(folder)])} unreadable")
        for name, reason in sorted(items):
            lines.append(f"  {name}  ::  {reason}")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")

    log.info("═" * 64)
    log.info(f"Unreadable: {n_bad} file(s) in {len(bad_by_album)} album(s) (of {total} scanned)")
    for folder in sorted(bad_by_album):
        log.info(f"  {len(bad_by_album[folder]):>3} × {Path(folder).name}")
    log.info(f"Full list: {out.resolve()}")
    log.info("═" * 64)


# ── CLI ─────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="Album-clustered music tagger (beets + Claude)")
    p.add_argument("directory", nargs="?", help="Root directory to scan")
    p.add_argument("--apply", action="store_true", help="Write tags (default: dry run)")
    p.add_argument("--limit", type=int, metavar="N", help="Process only the first N albums")
    p.add_argument("--model", default=verify.DEFAULT_MODEL, help=f"Claude model (default {verify.DEFAULT_MODEL})")
    p.add_argument("--db", default=DB_DEFAULT, help=f"SQLite cache/journal path (default {DB_DEFAULT})")
    p.add_argument("--confidence", type=int, default=70, metavar="N",
                   help="Min Claude confidence to write on --apply (default 70)")
    p.add_argument("--no-cache", action="store_true", help="Ignore cached lookups/verdicts")
    p.add_argument("--eval-only", action="store_true", help="Skip Claude; only show strong matches")
    p.add_argument("--list-runs", action="store_true", help="List apply runs available to undo")
    p.add_argument("--undo", metavar="RUN_ID", help="Restore original tags from a prior apply run")
    p.add_argument("--list-unreadable", action="store_true",
                   help="Read-only scan: list corrupt/unreadable audio files under <directory>")
    args = p.parse_args()

    if args.list_runs:
        cmd_list_runs(args.db)
        return
    if args.undo:
        cmd_undo(args.db, args.undo)
        return
    if args.list_unreadable:
        if not args.directory or not Path(args.directory).is_dir():
            p.error("--list-unreadable needs a valid directory")
        cmd_list_unreadable(args.directory)
        return

    if not args.directory or not Path(args.directory).is_dir():
        p.error("a valid directory is required (or use --list-runs / --undo)")

    if not args.eval_only and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — low/medium albums can't be verified. "
                    "Running with --eval-only behavior for this session.")
        args.eval_only = True

    try:
        _acquire_lock()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    run(args.directory, apply=args.apply, limit=args.limit, model=args.model,
        db_path=args.db, confidence=args.confidence,
        use_cache=not args.no_cache, eval_only=args.eval_only)


if __name__ == "__main__":
    main()
