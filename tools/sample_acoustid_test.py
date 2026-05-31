import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import acoustid
except ImportError as exc:
    print(f"Missing dependency: {exc}")
    sys.exit(1)

SUPPORTED_EXTENSIONS = {".mp3", ".flac"}


def load_acoustid_key() -> str:
    if os.environ.get("ACOUSTID_API_KEY"):
        return os.environ["ACOUSTID_API_KEY"]

    keys_path = Path(".venv/keys.txt")
    if not keys_path.exists():
        raise FileNotFoundError("No .venv/keys.txt found and ACOUSTID_API_KEY not set")

    with keys_path.open("r", encoding="utf-8") as f:
        for line in f:
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip().lower().startswith("acoustid"):
                return value.strip()
    raise ValueError("No AcoustID key found in .venv/keys.txt")


def collect_files(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED_EXTENSIONS]
    files.sort()
    return files


def run_lookup(filepath: Path, api_key: str) -> dict:
    record = {
        "file": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "error": None,
        "attempts": 0,
        "duration_seconds": 0.0,
        "result_count": 0,
        "top_candidate": None,
    }
    start = time.perf_counter()
    try:
        results = acoustid.match(
            api_key,
            str(filepath),
            meta="recordings releases releasegroups",
            parse=True,
        )
        record["attempts"] = 1
        candidates = []
        for score, recording_id, title, artist in results:
            candidates.append({
                "score": round(score, 4),
                "recording_id": recording_id,
                "title": title or "",
                "artist": artist or "",
            })
        record["result_count"] = len(candidates)
        record["top_candidate"] = candidates[0] if candidates else None
        record["candidates"] = candidates
    except acoustid.NoBackendError as exc:
        record["error"] = "NoBackendError"
        record["error_message"] = str(exc)
    except acoustid.FingerprintGenerationError as exc:
        record["error"] = "FingerprintGenerationError"
        record["error_message"] = str(exc)
    except acoustid.WebServiceError as exc:
        record["error"] = "WebServiceError"
        record["error_message"] = str(exc)
    except Exception as exc:
        record["error"] = type(exc).__name__
        record["error_message"] = str(exc)
    finally:
        record["duration_seconds"] = round(time.perf_counter() - start, 3)
    return record


def summarize(records: list[dict]) -> dict:
    summary = {
        "file_count": len(records),
        "success_count": 0,
        "no_backend_count": 0,
        "fingerprint_failure_count": 0,
        "webservice_error_count": 0,
        "other_error_count": 0,
        "avg_duration": 0.0,
        "avg_success_duration": 0.0,
        "success_rate": 0.0,
        "errors": {},
    }
    total_duration = 0.0
    total_success_duration = 0.0
    success_durations = 0
    for rec in records:
        total_duration += rec["duration_seconds"]
        if rec["error"] is None:
            summary["success_count"] += 1
            total_success_duration += rec["duration_seconds"]
            success_durations += 1
        elif rec["error"] == "NoBackendError":
            summary["no_backend_count"] += 1
        elif rec["error"] == "FingerprintGenerationError":
            summary["fingerprint_failure_count"] += 1
        elif rec["error"] == "WebServiceError":
            summary["webservice_error_count"] += 1
        else:
            summary["other_error_count"] += 1
        if rec["error"]:
            summary["errors"].setdefault(rec["error"], 0)
            summary["errors"][rec["error"]] += 1
    summary["avg_duration"] = round(total_duration / len(records), 3) if records else 0.0
    summary["avg_success_duration"] = round(total_success_duration / success_durations, 3) if success_durations else 0.0
    summary["success_rate"] = round(summary["success_count"] / len(records) * 100, 2) if records else 0.0
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample AcoustID viability test")
    parser.add_argument("root_dir", help="Root folder containing audio files")
    parser.add_argument("--out", default="sample_acoustid_results.json", help="Output JSON file")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of files to test")
    parser.add_argument("--rate", type=float, default=2.0, help="Requests per second")
    parser.add_argument("--quiet", action="store_true", help="Reduce console output")
    args = parser.parse_args()

    root = Path(args.root_dir)
    if not root.is_dir():
        print(f"Root path not found: {root}")
        return 1

    try:
        api_key = load_acoustid_key()
    except Exception as exc:
        print(f"Error loading AcoustID key: {exc}")
        return 1

    # Ensure .venv/Scripts is on PATH for fpcalc
    venv_scripts = Path(".venv") / "Scripts"
    if venv_scripts.exists():
        os.environ["PATH"] = str(venv_scripts) + os.pathsep + os.environ.get("PATH", "")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    files = collect_files(root)
    if not files:
        print("No MP3/FLAC files found.")
        return 1

    if args.limit > 0:
        files = files[:args.limit]

    print(f"Testing {len(files)} files under {root}")
    records = []
    interval = 1.0 / args.rate if args.rate > 0 else 0

    for idx, filepath in enumerate(files, start=1):
        if not args.quiet:
            print(f"[{idx}/{len(files)}] {filepath.name}")
        rec = run_lookup(filepath, api_key)
        records.append(rec)
        if rec["error"] and not args.quiet:
            print(json.dumps({
                "file": rec["file"],
                "error": rec["error"],
                "duration": rec["duration_seconds"],
                "error_message": rec.get("error_message"),
            }, ensure_ascii=False, indent=2))
        if idx < len(files) and interval > 0:
            time.sleep(interval)

    summary = summarize(records)
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "records": records}, f, indent=2, ensure_ascii=False)

    print("---\nSummary:")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Saved results to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
