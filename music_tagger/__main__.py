#!/usr/bin/env python3
"""
music_tagger.py
───────────────
Automated music metadata tagger that uses:
  1. AcoustID  – acoustic fingerprinting to identify tracks
  2. MusicBrainz – canonical metadata lookup
  3. Claude AI – replaces the human verification step in Picard

Usage:
  python music_tagger.py /path/to/music          # tag all MP3/FLAC recursively
  python music_tagger.py /path/to/music --dry-run # preview changes only
  python music_tagger.py /path/to/music --confidence 80  # stricter threshold

Requirements (install with pip):
  pip install pyacoustid musicbrainzngs mutagen anthropic

Also required (system binary):
  fpcalc  (part of Chromaprint) — https://acoustid.org/chromaprint
  macOS:    brew install chromaprint
  Ubuntu:   apt install libchromaprint-tools
  Windows:  download from acoustid.org/chromaprint

API keys required (both free):
  ACOUSTID_API_KEY  — register at https://acoustid.org/api-key
  ANTHROPIC_API_KEY — set in your environment
"""

import os
import sys
import io
import json
import time
import logging
import argparse
import atexit
import signal
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from datetime import datetime

# ── Ensure venv/Scripts is on PATH for fpcalc ─────────────────────────────────
venv_scripts = Path(__file__).parent.parent / ".venv" / "Scripts"
if venv_scripts.exists():
    os.environ["PATH"] = str(venv_scripts) + os.pathsep + os.environ.get("PATH", "")

KEYS_FILE_PATHS = [
    Path(".env/keys.txt"),
    Path(".venv/keys.txt"),
    Path("keys.txt"),
]


def load_keys_file() -> dict[str, str]:
    """Load API keys from a file if present."""
    for path in KEYS_FILE_PATHS:
        if path.is_file():
            data = {}
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        name, value = line.split("=", 1)
                    elif ":" in line:
                        name, value = line.split(":", 1)
                    else:
                        continue
                    data[name.strip()] = value.strip()
            return data
    return {}


def apply_key_file_values() -> None:
    values = load_keys_file()
    if not values:
        return

    os.environ.setdefault(
        "ACOUSTID_API_KEY",
        values.get("ACOUSTID_API_KEY")
        or values.get("acoustidKey")
        or values.get("acoustIdKey")
        or values.get("acoustid_key")
        or values.get("acoustidKEY"),
    )
    os.environ.setdefault(
        "ANTHROPIC_API_KEY",
        values.get("ANTHROPIC_API_KEY")
        or values.get("anthropicKey")
        or values.get("anthropic_api_key")
        or values.get("anthropicKEY"),
    )


apply_key_file_values()

# ── Third-party ──────────────────────────────────────────────────────────────
try:
    import acoustid
    import musicbrainzngs
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.id3 import (
        ID3, ID3NoHeaderError,
        TIT2, TPE1, TALB, TRCK, TDRC, TCON, TPE2, TPOS
    )
    import anthropic
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install pyacoustid musicbrainzngs mutagen anthropic")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPPORTED_EXTENSIONS = {".mp3", ".flac"}

# AcoustID web service guidance: do not exceed 3 requests per second.
# See https://acoustid.org/webservice for rate limit guidelines.
# Use 2 req/sec for safety with developer app key
ACOUSTID_RATE_LIMIT_PER_SECOND = 2
ACOUSTID_MIN_REQUEST_INTERVAL = 1.0 / ACOUSTID_RATE_LIMIT_PER_SECOND
ACOUSTID_MAX_RETRIES = 3
ACOUSTID_RETRY_BACKOFF_BASE = 1.0
_last_acoustid_request_time = 0.0

# Files below this Claude confidence score are skipped and logged for review
DEFAULT_CONFIDENCE_THRESHOLD = 75
ANTHROPIC_ESTIMATED_INPUT_TOKENS = 350
ANTHROPIC_ESTIMATED_OUTPUT_TOKENS = 250
ANTHROPIC_SONNET_INPUT_COST = 3 / 1_000_000
ANTHROPIC_SONNET_OUTPUT_COST = 15 / 1_000_000

# Single-instance lock to prevent concurrent runs from different terminals.
LOCKFILE_NAME = ".music_tagger.lock"


def _get_lockfile_path() -> Path:
    return Path(__file__).resolve().parent.parent / LOCKFILE_NAME


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_lockfile() -> None:
    lockfile = _get_lockfile_path()
    try:
        if lockfile.exists():
            lockfile.unlink()
    except OSError:
        pass


def _cleanup_and_exit(signum=None, frame=None) -> None:
    _remove_lockfile()
    if signum is not None:
        sys.exit(1)


def _acquire_lockfile() -> None:
    lockfile = _get_lockfile_path()
    if lockfile.exists():
        try:
            content = lockfile.read_text(encoding="utf-8").strip()
            existing_pid = int(content.split()[0])
        except Exception:
            existing_pid = None

        if existing_pid and _is_process_running(existing_pid):
            raise RuntimeError(
                f"Another music-tagger process is already running (PID={existing_pid}). "
                "Stop that process before starting a new one."
            )
        else:
            _remove_lockfile()

    fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
    atexit.register(_remove_lockfile)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _cleanup_and_exit)
        except Exception:
            pass

# MusicBrainz requires a user-agent
musicbrainzngs.set_useragent("MusicTaggerAI", "1.0", "music-tagger@local")

# ── Logging ───────────────────────────────────────────────────────────────────

log_path = Path("music_tagger.log")

# Create handlers with proper UTF-8 encoding support for Korean/emoji characters
file_handler = logging.FileHandler(log_path, encoding="utf-8")
stream_handler = logging.StreamHandler(
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
)
formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, stream_handler],
)
log = logging.getLogger(__name__)


# ── AcoustID / MusicBrainz ────────────────────────────────────────────────────

def _acoustid_rate_limit() -> None:
    global _last_acoustid_request_time
    now = time.time()
    elapsed = now - _last_acoustid_request_time
    wait = ACOUSTID_MIN_REQUEST_INTERVAL - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_acoustid_request_time = time.time()


def _acoustid_lookup(filepath: str, api_key: str):
    attempt = 1
    backoff = ACOUSTID_RETRY_BACKOFF_BASE
    while True:
        _acoustid_rate_limit()
        try:
            return acoustid.match(
                api_key,
                filepath,
                meta="recordings releases releasegroups",
                parse=True,
            )
        except acoustid.WebServiceError as e:
            message = str(e) or repr(e)
            if attempt >= ACOUSTID_MAX_RETRIES:
                log.error(
                    f"AcoustID web service failed after {attempt} attempts for {filepath}: {e!r}; args={e.args}"
                )
                raise

            log.warning(
                f"AcoustID web service error for {filepath}: {e!r}; args={e.args}. "
                f"Retrying in {backoff:.1f}s (attempt {attempt}/{ACOUSTID_MAX_RETRIES})"
            )
            time.sleep(backoff)
            backoff *= 2
            attempt += 1


def _score_acoustid_candidate(candidate: dict) -> tuple[int, float]:
    title = bool(candidate["title"].strip())
    artist = bool(candidate["artist"].strip())
    quality = 2 if title and artist else 1 if title or artist else 0
    return quality, candidate["acoustid_score"]


def get_acoustid_candidates(filepath: str, api_key: str) -> list[dict]:
    """
    Fingerprint a file and return MusicBrainz recording candidates.
    Each candidate has a score (0–1) and a list of possible recordings.
    """
    if not api_key:
        raise ValueError("ACOUSTID_API_KEY is not set")

    candidates = []
    try:
        results = _acoustid_lookup(filepath, api_key)
        for score, recording_id, title, artist in results:
            candidates.append({
                "acoustid_score": round(score, 3),
                "recording_id": recording_id,
                "title": title or "",
                "artist": artist or "",
            })

        # Prefer candidates with complete metadata and higher score.
        candidates.sort(key=_score_acoustid_candidate, reverse=True)
    except acoustid.NoBackendError:
        log.error("fpcalc not found — install Chromaprint and ensure fpcalc is on PATH")
    except acoustid.FingerprintGenerationError as e:
        log.warning(
            f"Fingerprint failed for {filepath}: {e!r}; args={e.args}"
        )
    except acoustid.WebServiceError as e:
        log.warning(
            f"AcoustID web service error for {filepath} after retries: {e!r}; args={e.args}"
        )

    return candidates


def enrich_from_musicbrainz(recording_id: str) -> dict:
    """
    Fetch full recording metadata from MusicBrainz for a given recording ID.
    Returns a flat dict of tag values.
    """
    time.sleep(1)  # MusicBrainz rate limit: 1 req/sec
    try:
        result = musicbrainzngs.get_recording_by_id(
            recording_id,
            includes=["artists", "releases"],
        )
        rec = result["recording"]

        # Pick the most suitable release (prefer studio albums over singles/compilations)
        releases = rec.get("release-list", [])
        chosen_release = _pick_best_release(releases)

        tags = {
            "title": rec.get("title", ""),
            "artist": _join_artists(rec.get("artist-credit", [])),
            "musicbrainz_recording_id": recording_id,
        }

        if chosen_release:
            tags.update({
                "album": chosen_release.get("title", ""),
                "albumartist": _join_artists(
                    chosen_release.get("artist-credit", rec.get("artist-credit", []))
                ),
                "date": chosen_release.get("date", "")[:4],   # year only
                "tracknumber": _get_track_number(chosen_release, recording_id),
                "discnumber": _get_disc_number(chosen_release, recording_id),
                "musicbrainz_albumid": chosen_release.get("id", ""),
            })

        return tags

    except musicbrainzngs.WebServiceError as e:
        log.warning(f"MusicBrainz lookup failed for {recording_id}: {e}")
        return {}


def _pick_best_release(releases: list) -> Optional[dict]:
    """Prefer official studio albums over singles, EPs, compilations."""
    if not releases:
        return None
    priority = {"Album": 0, "Single": 1, "EP": 2, "Compilation": 3}
    def rank(r):
        rg = r.get("release-group", {})
        ptype = rg.get("primary-type", "")
        return priority.get(ptype, 99)
    return min(releases, key=rank)


def _join_artists(credits: list) -> str:
    parts = []
    for c in credits:
        if isinstance(c, dict):
            parts.append(c.get("artist", {}).get("name", ""))
        elif isinstance(c, str):
            parts.append(c)
    return " & ".join(p for p in parts if p)


def _get_track_number(release: dict, recording_id: str) -> str:
    for medium in release.get("medium-list", []):
        for track in medium.get("track-list", []):
            if track.get("recording", {}).get("id") == recording_id:
                return track.get("number", "")
    return ""


def _get_disc_number(release: dict, recording_id: str) -> str:
    for i, medium in enumerate(release.get("medium-list", []), 1):
        for track in medium.get("track-list", []):
            if track.get("recording", {}).get("id") == recording_id:
                return str(i) if len(release.get("medium-list", [])) > 1 else ""
    return ""


# ── Existing tag reader ───────────────────────────────────────────────────────

def read_existing_tags(filepath: str) -> dict:
    """Read whatever tags already exist on the file."""
    ext = Path(filepath).suffix.lower()
    tags = {}
    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                return tags
            mapping = {
                "TIT2": "title", "TPE1": "artist", "TALB": "album",
                "TPE2": "albumartist", "TRCK": "tracknumber",
                "TDRC": "date", "TCON": "genre",
            }
            for frame, key in mapping.items():
                val = audio.get(frame)
                if val:
                    tags[key] = str(val)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in ["title", "artist", "album", "albumartist",
                        "tracknumber", "date", "genre"]:
                val = audio.get(key)
                if val:
                    tags[key] = val[0]
    except Exception as e:
        log.debug(f"Could not read existing tags from {filepath}: {e}")
    return tags


# ── Claude verification ───────────────────────────────────────────────────────

def ask_claude(
    filepath: str,
    existing_tags: dict,
    candidates: list[dict],
    client: anthropic.Anthropic,
) -> dict:
    """
    Ask Claude to pick the best MusicBrainz candidate and return final tags.
    Returns a dict with keys: confidence (int), tags (dict), reasoning (str).
    """
    prompt = f"""You are an expert music librarian helping tag audio files with accurate metadata.

FILE: {Path(filepath).name}

EXISTING TAGS (may be incomplete, wrong, or empty):
{json.dumps(existing_tags, indent=2) if existing_tags else "(none)"}

ACOUSTID + MUSICBRAINZ CANDIDATES (from audio fingerprinting):
{json.dumps(candidates, indent=2) if candidates else "(no fingerprint matches found)"}

Your task:
1. Evaluate whether the candidates are a genuine match for this file.
   - Use the filename, existing tags, AcoustID score, and MusicBrainz data as evidence.
   - Be especially skeptical of low AcoustID scores (< 0.6) or data-sparse candidates.
2. Select the best candidate, or reject all if none are trustworthy.
3. Propose final tag values, merging MusicBrainz data with any reliable existing tags.
4. Rate your confidence (0–100). Use:
   - 90–100: Strong fingerprint match + consistent metadata
   - 70–89:  Good match with minor uncertainty (e.g. multiple releases)
   - 50–69:  Plausible but something doesn't add up — flag for review
   - 0–49:   No reliable match — do not write tags

Respond ONLY with a JSON object, no prose, no markdown:
{{
  "confidence": <integer 0-100>,
  "action": "tag" | "skip" | "review",
  "reasoning": "<one or two sentences>",
  "selected_recording_id": "<mbid or null>",
  "tags": {{
    "title": "...",
    "artist": "...",
    "album": "...",
    "albumartist": "...",
    "tracknumber": "...",
    "discnumber": "...",
    "date": "...",
    "genre": "..."
  }}
}}

If action is "skip" or "review", tags may be empty or partial.
"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Tag writer ────────────────────────────────────────────────────────────────

def _normalize_tag(value: Optional[str]) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    # Normalize whitespace and remove punctuation so small format differences do not force Claude.
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _text_similarity(a: Optional[str], b: Optional[str]) -> float:
    a_norm = _normalize_tag(a)
    b_norm = _normalize_tag(b)
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def should_use_claude(existing_tags: dict, candidates: list[dict]) -> bool:
    """Return True when Claude should be asked to verify candidate tags."""
    if not candidates:
        return False
    if not existing_tags:
        return True

    top = candidates[0]
    required = ["title", "artist", "album"]
    if any(not existing_tags.get(k) for k in required):
        # If only the album is missing and title/artist already match very closely,
        # avoid Claude for a likely safe tag correction.
        if existing_tags.get("title") and existing_tags.get("artist"):
            title_match = _text_similarity(existing_tags.get("title"), top.get("title"))
            artist_match = _text_similarity(existing_tags.get("artist"), top.get("artist"))
            if title_match >= 0.9 and artist_match >= 0.9:
                return False
        return True

    title_match = _text_similarity(existing_tags.get("title"), top.get("title"))
    artist_match = _text_similarity(existing_tags.get("artist"), top.get("artist"))
    album_match = _text_similarity(existing_tags.get("album"), top.get("album"))

    if title_match >= 0.9 and artist_match >= 0.9 and album_match >= 0.9:
        return False

    # If title and artist already match closely and only album differs slightly,
    # skip Claude to reduce unnecessary verification.
    if title_match >= 0.95 and artist_match >= 0.95 and album_match >= 0.8:
        return False

    return True


def estimate_anthropic_cost(file_count: int,
                             input_tokens: int = ANTHROPIC_ESTIMATED_INPUT_TOKENS,
                             output_tokens: int = ANTHROPIC_ESTIMATED_OUTPUT_TOKENS) -> float:
    """Estimate Anthropic cost for a number of Sonnet calls."""
    per_file = (input_tokens * ANTHROPIC_SONNET_INPUT_COST) + (
        output_tokens * ANTHROPIC_SONNET_OUTPUT_COST
    )
    return round(file_count * per_file, 4)


def evaluate_file_for_claude(filepath: str, acoustid_key: str) -> dict:
    existing_tags = read_existing_tags(filepath)
    raw_candidates = get_acoustid_candidates(filepath, acoustid_key)
    enriched_candidates = []
    for c in raw_candidates[:3]:
        mb_data = enrich_from_musicbrainz(c["recording_id"]) if c["recording_id"] else {}
        enriched_candidates.append({**c, **mb_data})

    needs_claude = bool(enriched_candidates) and should_use_claude(existing_tags, enriched_candidates)
    reason = (
        "Needs Claude verification"
        if needs_claude
        else "Existing tags appear to match the top candidate"
    )
    if not enriched_candidates:
        reason = "No AcoustID/MusicBrainz candidates"

    return {
        "file": filepath,
        "existing_tags": existing_tags,
        "raw_candidates": raw_candidates,
        "enriched_candidates": enriched_candidates,
        "needs_claude": needs_claude,
        "evaluation_reason": reason,
    }


def write_tags(filepath: str, tags: dict, dry_run: bool = False) -> bool:
    """Write tag dict to an MP3 or FLAC file."""
    ext = Path(filepath).suffix.lower()
    clean = {k: v for k, v in tags.items() if v}

    if dry_run:
        log.info(f"  [DRY RUN] Would write: {json.dumps(clean)}")
        return True

    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                audio = ID3()

            frame_map = {
                "title": TIT2, "artist": TPE1, "album": TALB,
                "albumartist": TPE2, "tracknumber": TRCK,
                "date": TDRC, "genre": TCON, "discnumber": TPOS,
            }
            for key, Frame in frame_map.items():
                if clean.get(key):
                    audio[Frame.__name__] = Frame(encoding=3, text=clean[key])
            audio.save(filepath)

        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in ["title", "artist", "album", "albumartist",
                        "tracknumber", "discnumber", "date", "genre"]:
                if clean.get(key):
                    audio[key] = clean[key]
            audio.save()

        return True

    except Exception as e:
        log.error(f"  Failed to write tags to {filepath}: {e}")
        return False


# ── Main processing loop ──────────────────────────────────────────────────────

def process_file(
    filepath: str,
    acoustid_key: str,
    claude_client: anthropic.Anthropic,
    confidence_threshold: int,
    dry_run: bool,
    precomputed: dict | None = None,
) -> dict:
    """Process a single audio file. Returns a result summary dict."""
    result = {
        "file": filepath,
        "status": "unknown",
        "confidence": 0,
        "reasoning": "",
        "tags_written": {},
        "existing_tags": {},
        "evaluation_reason": "",
    }

    log.info(f"Processing: {Path(filepath).name}")

    if precomputed is not None:
        existing_tags = precomputed["existing_tags"]
        raw_candidates = precomputed["raw_candidates"]
        enriched_candidates = precomputed["enriched_candidates"]
    else:
        existing_tags = read_existing_tags(filepath)
        raw_candidates = get_acoustid_candidates(filepath, acoustid_key)
        log.info(f"  AcoustID: {len(raw_candidates)} candidate(s)")
        enriched_candidates = []
        for c in raw_candidates[:3]:
            mb_data = enrich_from_musicbrainz(c["recording_id"]) if c["recording_id"] else {}
            enriched_candidates.append({**c, **mb_data})

    result["existing_tags"] = existing_tags
    result["evaluation_reason"] = (
        precomputed["evaluation_reason"] if precomputed is not None else ""
    )

    if not enriched_candidates:
        result["status"] = "skipped"
        result["reasoning"] = "No AcoustID/MusicBrainz candidates"
        log.info("  No MusicBrainz candidates found; skipping Claude.")
        return result

    if not should_use_claude(existing_tags, enriched_candidates):
        result["status"] = "skipped"
        result["reasoning"] = "Existing tags appear to match the top candidate"
        log.info("  Existing tags match top candidate; skipping Claude.")
        return result

    # 4. Ask Claude
    try:
        verdict = ask_claude(filepath, existing_tags, enriched_candidates, claude_client)
    except (json.JSONDecodeError, Exception) as e:
        log.error(f"  Claude error: {e}")
        result["status"] = "error"
        result["reasoning"] = str(e)
        return result

    confidence = verdict.get("confidence", 0)
    action = verdict.get("action", "skip")
    reasoning = verdict.get("reasoning", "")
    tags = verdict.get("tags", {})

    result["confidence"] = confidence
    result["reasoning"] = reasoning

    log.info(f"  Claude: {action.upper()} | confidence={confidence} | {reasoning}")

    # 5. Act on verdict
    if action == "tag" and confidence >= confidence_threshold:
        success = write_tags(filepath, tags, dry_run=dry_run)
        result["status"] = "tagged" if success else "error"
        result["tags_written"] = tags
        log.info(f"  ✓ Tags {'previewed' if dry_run else 'written'}: "
                 f"{tags.get('artist','?')} – {tags.get('title','?')} "
                 f"({tags.get('album','?')})")
    elif action == "review" or confidence < confidence_threshold:
        result["status"] = "needs_review"
        log.info(f"  ⚠ Flagged for manual review (confidence {confidence} < {confidence_threshold})")
    else:
        result["status"] = "skipped"
        log.info(f"  ✗ Skipped: {reasoning}")

    return result


def save_reports(results: list[dict], evaluations: list[dict] | None = None):
    report_path = Path("music_tagger_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    changed_files = [r for r in results if r["status"] == "tagged"]
    html_path = Path("music_tagger_report.html")
    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    eval_count = len([e for e in evaluations or [] if e.get("needs_claude")])
    total_files = len(evaluations or results)

    rows = []
    for r in changed_files:
        before = r.get("existing_tags", {})
        after = r.get("tags_written", {})
        rows.append(
            f"<tr><td>{Path(r['file']).name}</td>"
            f"<td>{r['confidence']}</td>"
            f"<td>{r['reasoning']}</td>"
            f"<td><pre>{json.dumps(before, indent=2)}</pre></td>"
            f"<td><pre>{json.dumps(after, indent=2)}</pre></td></tr>"
        )

    changed_section = (
        "<p>No files were changed.</p>"
        if not rows
        else (
            "<table border=1 cellpadding=6 cellspacing=0>"
            "<thead><tr><th>File</th><th>Confidence</th><th>Reason</th>"
            "<th>Existing tags</th><th>Written tags</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    )

    html = f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>MusicTagger Report</title>
<style>body{{font-family:Arial,sans-serif;margin:20px;}}table{{border-collapse:collapse;width:100%;}}th,td{{border:1px solid #999;padding:8px;vertical-align:top;text-align:left;}}pre{{white-space:pre-wrap;word-wrap:break-word;margin:0;font-family:Consolas,monospace;font-size:12px;}}</style>
</head>
<body>
<h1>MusicTagger Report</h1>
<p>Generated: {report_time}</p>
<p>Total files scanned: {total_files}</p>
<p>Files requiring Claude: {eval_count}</p>
<p>Files changed: {len(changed_files)}</p>
<h2>Files changed</h2>
{changed_section}
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    log.info(f"\nJSON report saved to: {report_path}")
    log.info(f"HTML report saved to: {html_path}")


def process_directory(
    root_dir: str,
    acoustid_key: str,
    confidence_threshold: int,
    dry_run: bool,
):
    """Walk a directory tree and process all supported audio files."""
    claude_client = anthropic.Anthropic()

    audio_files = [
        str(p)
        for p in Path(root_dir).rglob("*")
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not audio_files:
        log.warning(f"No MP3 or FLAC files found in {root_dir}")
        return

    log.info(f"Found {len(audio_files)} file(s) to process")
    if dry_run:
        log.info("DRY RUN mode — no files will be modified")

    evaluations = []
    for filepath in audio_files:
        log.info(f"Evaluating: {Path(filepath).name}")
        evaluation = evaluate_file_for_claude(filepath, acoustid_key)
        evaluations.append(evaluation)
        time.sleep(0.2)

    candidates = [e for e in evaluations if e["needs_claude"]]
    estimate = estimate_anthropic_cost(len(candidates))

    log.info("\n" + "═" * 60)
    log.info("PRE-RUN EVALUATION")
    log.info("═" * 60)
    log.info(f"  Total files scanned        : {len(evaluations)}")
    log.info(f"  Files requiring Claude     : {len(candidates)}")
    log.info(f"  Estimated Anthropic cost  : ${estimate:.4f}")
    log.info("═" * 60)

    if not candidates:
        log.info("No files require Claude verification. Running file processing only.")
    elif sys.stdin.isatty():
        answer = input(
            "Proceed to consult Claude for these files? [y/N]: "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            log.info("Aborted by user before Claude verification.")
            save_reports([], evaluations)
            return
    else:
        log.info("Non-interactive mode; continuing with Claude verification.")

    results = []
    for evaluation in evaluations:
        result = process_file(
            evaluation["file"],
            acoustid_key,
            claude_client,
            confidence_threshold,
            dry_run,
            precomputed=evaluation,
        )
        results.append(result)
        time.sleep(0.5)  # Gentle pacing

    # ── Summary report ────────────────────────────────────────────────────────
    tagged      = [r for r in results if r["status"] == "tagged"]
    review      = [r for r in results if r["status"] == "needs_review"]
    skipped     = [r for r in results if r["status"] == "skipped"]
    errors      = [r for r in results if r["status"] == "error"]

    log.info("\n" + "═" * 60)
    log.info(f"  SUMMARY  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    log.info("═" * 60)
    log.info(f"  Total files   : {len(results)}")
    log.info(f"  Tagged        : {len(tagged)}")
    log.info(f"  Needs review  : {len(review)}")
    log.info(f"  Skipped       : {len(skipped)}")
    log.info(f"  Errors        : {len(errors)}")
    log.info("═" * 60)

    if review:
        log.info("\nFiles needing manual review:")
        for r in review:
            log.info(f"  [{r['confidence']:3d}%] {Path(r['file']).name}")
            log.info(f"         {r['reasoning']}")

    save_reports(results, evaluations)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Automatically tag MP3/FLAC files using AcoustID + MusicBrainz + Claude AI"
    )
    parser.add_argument("directory", help="Root directory to scan")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing any tags"
    )
    parser.add_argument(
        "--confidence", type=int, default=DEFAULT_CONFIDENCE_THRESHOLD,
        metavar="N",
        help=f"Minimum Claude confidence score to write tags (default: {DEFAULT_CONFIDENCE_THRESHOLD})"
    )
    parser.add_argument(
        "--acoustid-key", default=ACOUSTID_API_KEY,
        help="AcoustID API key (or set ACOUSTID_API_KEY env var)"
    )
    args = parser.parse_args()

    if not args.acoustid_key:
        print("Error: AcoustID API key required.")
        print("  Register free at https://acoustid.org/api-key")
        print("  Then: export ACOUSTID_API_KEY=your_key_here")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    if not Path(args.directory).is_dir():
        print(f"Error: {args.directory} is not a directory")
        sys.exit(1)

    try:
        _acquire_lockfile()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    process_directory(
        root_dir=args.directory,
        acoustid_key=args.acoustid_key,
        confidence_threshold=args.confidence,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()