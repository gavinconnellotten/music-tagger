#!/usr/bin/env python3
"""
music_tagger/__main__.py
─────────────────────────
Automated music metadata tagger using:
  1. AcoustID  – acoustic fingerprinting to identify tracks
  2. MusicBrainz – canonical metadata lookup
  3. Claude AI – metadata verification and selection

Usage:
  python -m music_tagger /path/to/music --dry-run
  python -m music_tagger /path/to/music --dry-run --limit 20
  python -m music_tagger /path/to/music --confidence 80
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
import socket
import re

# Global timeout for all HTTP requests (AcoustID, MusicBrainz).
# Prevents the process hanging indefinitely on a stalled connection.
socket.setdefaulttimeout(30)
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from datetime import datetime

# ── Ensure venv/Scripts is on PATH for fpcalc ──────────────────────────────────
venv_scripts = Path(__file__).parent.parent / ".venv" / "Scripts"
if venv_scripts.exists():
    os.environ["PATH"] = str(venv_scripts) + os.pathsep + os.environ.get("PATH", "")

KEYS_FILE_PATHS = [
    Path(".env/keys.txt"),
    Path(".venv/keys.txt"),
    Path("keys.txt"),
]


def load_keys_file() -> dict[str, str]:
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

# ── Third-party ────────────────────────────────────────────────────────────────
try:
    import acoustid
    import musicbrainzngs
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

# ── Config ─────────────────────────────────────────────────────────────────────

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPPORTED_EXTENSIONS = {".mp3", ".flac"}

ACOUSTID_RATE_LIMIT_PER_SECOND = 2
ACOUSTID_MIN_REQUEST_INTERVAL = 1.0 / ACOUSTID_RATE_LIMIT_PER_SECOND
ACOUSTID_MAX_RETRIES = 3
ACOUSTID_RETRY_BACKOFF_BASE = 1.0
_last_acoustid_request_time = 0.0

DEFAULT_CONFIDENCE_THRESHOLD = 75
ANTHROPIC_ESTIMATED_INPUT_TOKENS = 350
ANTHROPIC_ESTIMATED_OUTPUT_TOKENS = 250
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_SONNET_INPUT_COST = 3 / 1_000_000
ANTHROPIC_SONNET_OUTPUT_COST = 15 / 1_000_000

LOCKFILE_NAME = ".music_tagger.lock"

# Fields reported as missing when blank
CHECKED_FIELDS = ["title", "artist", "album", "albumartist", "tracknumber", "date", "genre", "discnumber"]
# Fields compared against AcoustID candidates for mismatch detection
COMPARED_FIELDS = ["title", "artist", "album", "albumartist"]
MISMATCH_THRESHOLD = 0.8

# Fingerprint status constants
FP_OK = "ok"
FP_NO_MATCH = "no_match"
FP_API_ERROR = "api_error"
FP_FAILED = "fingerprint_failed"
FP_NO_BACKEND = "no_backend"


# ── Process lock ───────────────────────────────────────────────────────────────

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
    except (OSError, AttributeError):
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

    fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
    atexit.register(_remove_lockfile)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _cleanup_and_exit)
        except Exception:
            pass


musicbrainzngs.set_useragent("MusicTaggerAI", "1.0", "music-tagger@local")

# ── Logging ────────────────────────────────────────────────────────────────────

log_path = Path("music_tagger.log")

# Rotate log at startup so each run starts fresh
if log_path.exists() and log_path.stat().st_size > 0:
    backup = log_path.with_suffix(".log.bak")
    try:
        if backup.exists():
            backup.unlink()
        log_path.rename(backup)
    except OSError:
        pass

file_handler = logging.FileHandler(log_path, encoding="utf-8")
stream_handler = logging.StreamHandler(
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
)
formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
log = logging.getLogger(__name__)


# ── AcoustID / MusicBrainz ─────────────────────────────────────────────────────

def _acoustid_rate_limit() -> None:
    global _last_acoustid_request_time
    now = time.time()
    wait = ACOUSTID_MIN_REQUEST_INTERVAL - (now - _last_acoustid_request_time)
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
            if attempt >= ACOUSTID_MAX_RETRIES:
                log.error(
                    f"AcoustID failed after {attempt} attempts for {Path(filepath).name}: {e!r}"
                )
                raise
            log.warning(
                f"AcoustID retry {attempt}/{ACOUSTID_MAX_RETRIES} for {Path(filepath).name}: {e!r}. "
                f"Waiting {backoff:.1f}s"
            )
            time.sleep(backoff)
            backoff *= 2
            attempt += 1


def _score_acoustid_candidate(candidate: dict) -> tuple[int, float]:
    title = bool(candidate["title"].strip())
    artist = bool(candidate["artist"].strip())
    quality = 2 if title and artist else 1 if title or artist else 0
    return quality, candidate["acoustid_score"]


def get_acoustid_candidates(filepath: str, api_key: str) -> tuple[list[dict], str]:
    """
    Fingerprint a file and return (candidates, fp_status).
    fp_status: ok | no_match | api_error | fingerprint_failed | no_backend
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
        candidates.sort(key=_score_acoustid_candidate, reverse=True)
        return candidates, (FP_OK if candidates else FP_NO_MATCH)

    except acoustid.NoBackendError:
        log.error("fpcalc not found — install Chromaprint and ensure fpcalc is on PATH")
        return [], FP_NO_BACKEND
    except acoustid.FingerprintGenerationError as e:
        log.warning(f"Fingerprint failed for {Path(filepath).name}: {e!r}")
        return [], FP_FAILED
    except acoustid.WebServiceError as e:
        log.warning(f"AcoustID API error for {Path(filepath).name} after retries: {e!r}")
        return [], FP_API_ERROR


def enrich_from_musicbrainz(recording_id: str) -> dict:
    time.sleep(1)  # MusicBrainz rate limit: 1 req/sec
    try:
        result = musicbrainzngs.get_recording_by_id(
            recording_id,
            includes=["artists", "releases"],
        )
        rec = result["recording"]
        chosen_release = _pick_best_release(rec.get("release-list", []))

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
                "date": chosen_release.get("date", "")[:4],
                "tracknumber": _get_track_number(chosen_release, recording_id),
                "discnumber": _get_disc_number(chosen_release, recording_id),
                "musicbrainz_albumid": chosen_release.get("id", ""),
            })
        return tags

    except musicbrainzngs.WebServiceError as e:
        log.warning(f"MusicBrainz lookup failed for {recording_id}: {e}")
        return {}
    except socket.timeout:
        log.warning(f"MusicBrainz request timed out for {recording_id}")
        return {}
    except Exception as e:
        log.warning(f"MusicBrainz unexpected error for {recording_id}: {e}")
        return {}


def _pick_best_release(releases: list) -> Optional[dict]:
    if not releases:
        return None
    priority = {"Album": 0, "Single": 1, "EP": 2, "Compilation": 3}
    return min(releases, key=lambda r: priority.get(r.get("release-group", {}).get("primary-type", ""), 99))


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


# ── Tag reader ──────────────────────────────────────────────────────────────────

def read_existing_tags(filepath: str) -> dict:
    ext = Path(filepath).suffix.lower()
    tags = {}
    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                return tags
            for frame, key in {
                "TIT2": "title", "TPE1": "artist", "TALB": "album",
                "TPE2": "albumartist", "TRCK": "tracknumber",
                "TDRC": "date", "TCON": "genre", "TPOS": "discnumber",
            }.items():
                val = audio.get(frame)
                if val:
                    tags[key] = str(val)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in ["title", "artist", "album", "albumartist",
                        "tracknumber", "discnumber", "date", "genre"]:
                val = audio.get(key)
                if val:
                    tags[key] = val[0]
    except Exception as e:
        log.debug(f"Could not read existing tags from {filepath}: {e}")
    return tags


# ── Tag problem classification ──────────────────────────────────────────────────

def _normalize_tag(value: Optional[str]) -> str:
    if not value:
        return ""
    text = re.sub(r"[^\w\s]", "", str(value).strip().lower())
    return re.sub(r"\s+", " ", text)


def _text_similarity(a: Optional[str], b: Optional[str]) -> float:
    a_norm = _normalize_tag(a)
    b_norm = _normalize_tag(b)
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def classify_tag_problems(
    existing_tags: dict,
    enriched_candidates: list[dict],
    fp_status: str,
) -> dict:
    """
    Returns {missing, mismatched, overall_status} for a file.
    overall_status: ok | needs_attention | unresolvable | fingerprint_failed
    """
    missing = [f for f in CHECKED_FIELDS if not existing_tags.get(f)]

    mismatched = []
    if enriched_candidates:
        top = enriched_candidates[0]
        for field in COMPARED_FIELDS:
            existing = existing_tags.get(field)
            candidate = top.get(field)
            if existing and candidate and _text_similarity(existing, candidate) < MISMATCH_THRESHOLD:
                mismatched.append(field)

    if fp_status in (FP_FAILED, FP_NO_BACKEND):
        overall = "fingerprint_failed"
    elif fp_status in (FP_NO_MATCH, FP_API_ERROR):
        overall = "unresolvable"
    elif missing or mismatched:
        overall = "needs_attention"
    else:
        overall = "ok"

    return {"missing": missing, "mismatched": mismatched, "overall_status": overall}


# ── Claude verification ─────────────────────────────────────────────────────────

def _parse_claude_json(raw: str) -> dict:
    """Extract and parse a JSON object from Claude's response, tolerating markdown fences or prose."""
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find the first {...} block in case Claude wrapped the JSON in prose
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise json.JSONDecodeError("No JSON object found in Claude response", raw, 0)


def ask_claude(
    filepath: str,
    existing_tags: dict,
    candidates: list[dict],
    client: anthropic.Anthropic,
) -> dict:
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
4. Rate your confidence (0-100):
   - 90-100: Strong fingerprint match + consistent metadata
   - 70-89:  Good match with minor uncertainty
   - 50-69:  Plausible but something doesn't add up
   - 0-49:   No reliable match — do not write tags

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
        model=ANTHROPIC_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    try:
        return _parse_claude_json(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.error(f"Failed to parse Claude response: {e}\nRaw response: {raw[:300]}")
        raise


# ── Should-use-Claude heuristic ─────────────────────────────────────────────────

def should_use_claude(existing_tags: dict, candidates: list[dict]) -> bool:
    if not candidates:
        return False
    if not existing_tags:
        return True

    top = candidates[0]
    required = ["title", "artist", "album"]
    if any(not existing_tags.get(k) for k in required):
        # Special case: if only album is missing but title+artist match closely, skip Claude
        if existing_tags.get("title") and existing_tags.get("artist"):
            if (_text_similarity(existing_tags.get("title"), top.get("title")) >= 0.9
                    and _text_similarity(existing_tags.get("artist"), top.get("artist")) >= 0.9):
                return False
        return True

    title_sim = _text_similarity(existing_tags.get("title"), top.get("title"))
    artist_sim = _text_similarity(existing_tags.get("artist"), top.get("artist"))
    album_sim = _text_similarity(existing_tags.get("album"), top.get("album", ""))

    if title_sim >= 0.9 and artist_sim >= 0.9 and album_sim >= 0.9:
        return False
    if title_sim >= 0.95 and artist_sim >= 0.95 and album_sim >= 0.8:
        return False

    return True


# ── Cost estimation ─────────────────────────────────────────────────────────────

def estimate_anthropic_cost(
    file_count: int,
    input_tokens: int = ANTHROPIC_ESTIMATED_INPUT_TOKENS,
    output_tokens: int = ANTHROPIC_ESTIMATED_OUTPUT_TOKENS,
) -> float:
    per_file = (input_tokens * ANTHROPIC_SONNET_INPUT_COST) + (output_tokens * ANTHROPIC_SONNET_OUTPUT_COST)
    return round(file_count * per_file, 4)


# ── Pre-evaluation pass ─────────────────────────────────────────────────────────

def evaluate_file_for_claude(filepath: str, acoustid_key: str) -> dict:
    existing_tags = read_existing_tags(filepath)
    raw_candidates, fp_status = get_acoustid_candidates(filepath, acoustid_key)

    enriched_candidates = []
    for c in raw_candidates[:3]:
        if c.get("recording_id"):
            mb_data = enrich_from_musicbrainz(c["recording_id"])
            enriched_candidates.append({**c, **mb_data})
        else:
            enriched_candidates.append(c)

    problems = classify_tag_problems(existing_tags, enriched_candidates, fp_status)
    needs_claude = bool(enriched_candidates) and should_use_claude(existing_tags, enriched_candidates)

    if fp_status in (FP_FAILED, FP_NO_BACKEND):
        reason = f"Fingerprint generation failed ({fp_status})"
    elif fp_status in (FP_NO_MATCH, FP_API_ERROR):
        reason = f"No AcoustID match ({fp_status})"
    elif needs_claude:
        reason = "Needs Claude verification"
    else:
        reason = "Existing tags appear correct"

    return {
        "file": filepath,
        "existing_tags": existing_tags,
        "raw_candidates": raw_candidates,
        "enriched_candidates": enriched_candidates,
        "fp_status": fp_status,
        "needs_claude": needs_claude,
        "evaluation_reason": reason,
        "problems": problems,
    }


# ── Tag writer ──────────────────────────────────────────────────────────────────

def write_tags(filepath: str, tags: dict, dry_run: bool = False) -> bool:
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


# ── Main processing loop ────────────────────────────────────────────────────────

def process_file(
    filepath: str,
    acoustid_key: str,
    claude_client: anthropic.Anthropic,
    confidence_threshold: int,
    dry_run: bool,
    precomputed: dict | None = None,
) -> dict:
    result = {
        "file": filepath,
        "status": "unknown",
        "confidence": None,
        "reasoning": "",
        "tags_written": {},
        "existing_tags": {},
        "evaluation_reason": "",
        "problems": {},
        "fp_status": FP_OK,
    }

    log.info(f"Processing: {Path(filepath).name}")

    if precomputed is not None:
        existing_tags = precomputed["existing_tags"]
        raw_candidates = precomputed["raw_candidates"]
        enriched_candidates = precomputed["enriched_candidates"]
        fp_status = precomputed.get("fp_status", FP_OK)
        result["problems"] = precomputed.get("problems", {})
        result["evaluation_reason"] = precomputed.get("evaluation_reason", "")
        result["fp_status"] = fp_status
    else:
        existing_tags = read_existing_tags(filepath)
        raw_candidates, fp_status = get_acoustid_candidates(filepath, acoustid_key)
        log.info(f"  AcoustID: {len(raw_candidates)} candidate(s) [{fp_status}]")
        enriched_candidates = []
        for c in raw_candidates[:3]:
            if c.get("recording_id"):
                mb_data = enrich_from_musicbrainz(c["recording_id"])
                enriched_candidates.append({**c, **mb_data})
            else:
                enriched_candidates.append(c)
        result["problems"] = classify_tag_problems(existing_tags, enriched_candidates, fp_status)
        result["fp_status"] = fp_status

    result["existing_tags"] = existing_tags

    if fp_status in (FP_FAILED, FP_NO_BACKEND):
        result["status"] = "fingerprint_failed"
        result["reasoning"] = f"Could not fingerprint file ({fp_status})"
        log.info(f"  Fingerprint failed ({fp_status}); skipping.")
        return result

    if not enriched_candidates:
        result["status"] = "unresolvable"
        result["reasoning"] = f"No AcoustID match ({fp_status})"
        log.info(f"  No candidates found [{fp_status}]; skipping.")
        return result

    if not should_use_claude(existing_tags, enriched_candidates):
        overall = result["problems"].get("overall_status", "ok")
        result["status"] = "ok" if overall == "ok" else "skipped"
        result["reasoning"] = "Existing tags match top candidate"
        log.info("  Tags look good; skipping Claude.")
        return result

    # Ask Claude
    try:
        verdict = ask_claude(filepath, existing_tags, enriched_candidates, claude_client)
    except Exception as e:
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

    if dry_run:
        # Capture Claude's full proposal regardless of confidence
        result["tags_written"] = tags
        if action == "tag":
            result["status"] = "would_tag"
        elif action == "review":
            result["status"] = "would_review"
        else:
            result["status"] = "would_skip"
        write_tags(filepath, tags, dry_run=True)
        log.info(
            f"  [DRY RUN] Proposes: {tags.get('artist','?')} – "
            f"{tags.get('title','?')} ({tags.get('album','?')})"
        )
    else:
        if action == "tag" and confidence >= confidence_threshold:
            success = write_tags(filepath, tags, dry_run=False)
            result["status"] = "tagged" if success else "error"
            result["tags_written"] = tags
            log.info(f"  ✓ Tagged: {tags.get('artist','?')} – {tags.get('title','?')}")
        elif action == "review" or confidence < confidence_threshold:
            result["status"] = "needs_review"
            log.info(f"  ⚠ Needs review (confidence={confidence} < {confidence_threshold})")
        else:
            result["status"] = "skipped"
            log.info(f"  ✗ Skipped: {reasoning}")

    return result


# ── Reports ─────────────────────────────────────────────────────────────────────

def save_reports(results: list[dict], evaluations: list[dict] | None = None, dry_run: bool = False):
    report_path = Path("music_tagger_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    _save_html_report(results, dry_run)

    log.info(f"JSON report: {report_path.resolve()}")
    log.info(f"HTML report: {Path('music_tagger_report.html').resolve()}")


def _format_tags_html(tags: dict) -> str:
    if not tags:
        return "<em>(none)</em>"
    lines = [
        f"<b>{k}:</b> {v}"
        for k, v in tags.items()
        if v and not k.startswith("musicbrainz_")
    ]
    return "<br>".join(lines) if lines else "<em>(none)</em>"


def _save_html_report(results: list[dict], dry_run: bool) -> None:
    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(results)

    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    ok_count = status_counts.get("ok", 0) + status_counts.get("skipped", 0)
    fp_failed_count = status_counts.get("fingerprint_failed", 0)
    unresolvable_count = status_counts.get("unresolvable", 0)
    error_count = status_counts.get("error", 0)

    if dry_run:
        claude_statuses = {"would_tag", "would_review", "would_skip"}
    else:
        claude_statuses = {"tagged", "needs_review"}
    claude_count = sum(1 for r in results if r["status"] in claude_statuses)
    estimate = estimate_anthropic_cost(claude_count)

    mode_label = "DRY RUN — no files were modified" if dry_run else "LIVE RUN"
    mode_color = "#d35400" if dry_run else "#27ae60"

    summary_html = f"""
<div class="summary">
  <h2>Summary</h2>
  <p style="font-weight:bold;color:{mode_color};font-size:1.1em;">{'⚠ ' if dry_run else ''}{mode_label}</p>
  <table class="summary-table">
    <tr><th>Category</th><th>Count</th></tr>
    <tr><td>Total files scanned</td><td><strong>{total}</strong></td></tr>
    <tr style="color:#27ae60"><td>✓ Tags correct (Claude skipped)</td><td>{ok_count}</td></tr>
    <tr style="color:#e67e22"><td>⚠ Needs attention (Claude {"would be" if dry_run else "was"} consulted)</td><td>{claude_count}</td></tr>
    <tr style="color:#95a5a6"><td>✗ AcoustID no match / API error</td><td>{unresolvable_count}</td></tr>
    <tr style="color:#c0392b"><td>✗ Fingerprint failed</td><td>{fp_failed_count}</td></tr>
    {'<tr style="color:#c0392b"><td>⚠ Errors</td><td>' + str(error_count) + '</td></tr>' if error_count else ''}
  </table>
  <p style="background:#eaf4fb;padding:10px;border-radius:4px;display:inline-block;margin-top:10px;">
    Estimated Claude API cost: <strong>${estimate:.4f}</strong>
    &nbsp;({claude_count} file(s) × ~{ANTHROPIC_ESTIMATED_INPUT_TOKENS} in /
    ~{ANTHROPIC_ESTIMATED_OUTPUT_TOKENS} out tokens at {ANTHROPIC_MODEL})
  </p>
</div>"""

    # Problem files = everything that isn't cleanly OK or fingerprint-failed (shown separately)
    problem_files = [
        r for r in results
        if r["status"] not in ("ok", "skipped", "fingerprint_failed")
    ]

    if problem_files:
        rows = []
        for r in problem_files:
            fname = Path(r["file"]).name
            status = r["status"]
            confidence = r.get("confidence")
            conf_str = f"{confidence}%" if confidence is not None else "—"
            reasoning = r.get("reasoning", "—")
            problems = r.get("problems", {})
            missing_str = ", ".join(problems.get("missing", [])) or "—"
            mismatch_str = ", ".join(problems.get("mismatched", [])) or "—"
            existing_html = _format_tags_html(r.get("existing_tags", {}))
            proposed_html = _format_tags_html(r.get("tags_written", {})) if r.get("tags_written") else "<em>(no proposal)</em>"

            status_color = {
                "would_tag": "#27ae60",
                "tagged": "#27ae60",
                "would_review": "#e67e22",
                "needs_review": "#e67e22",
                "would_skip": "#95a5a6",
                "unresolvable": "#95a5a6",
                "error": "#c0392b",
            }.get(status, "#333")

            status_label = {
                "would_tag": "Would tag",
                "would_review": "Would review",
                "would_skip": "Would skip (low conf.)",
                "tagged": "Tagged",
                "needs_review": "Needs review",
                "unresolvable": "No match",
                "error": "Error",
            }.get(status, status)

            rows.append(
                f"<tr>"
                f"<td class='fn'>{fname}</td>"
                f"<td style='color:{status_color};font-weight:bold;white-space:nowrap'>{status_label}</td>"
                f"<td>{missing_str}</td>"
                f"<td>{mismatch_str}</td>"
                f"<td class='tc'>{existing_html}</td>"
                f"<td class='tc'>{proposed_html}</td>"
                f"<td style='text-align:center'>{conf_str}</td>"
                f"<td class='rs'>{reasoning}</td>"
                f"</tr>"
            )

        problem_section = f"""
<h2>Problem Files ({len(problem_files)})</h2>
<table class="ft">
  <thead>
    <tr>
      <th style="width:14%">File</th>
      <th style="width:9%">Status</th>
      <th style="width:10%">Missing fields</th>
      <th style="width:10%">Mismatched fields</th>
      <th style="width:18%">Current tags</th>
      <th style="width:18%">Claude proposes</th>
      <th style="width:5%">Conf.</th>
      <th style="width:16%">Reasoning</th>
    </tr>
  </thead>
  <tbody>{"".join(rows)}</tbody>
</table>"""
    else:
        problem_section = "<p>No problem files — all tags look correct!</p>"

    # Fingerprint-failed files (separate section, no Claude column needed)
    failed_files = [r for r in results if r["status"] == "fingerprint_failed"]
    if failed_files:
        failed_rows = "".join(
            f"<tr><td class='fn'>{Path(r['file']).name}</td>"
            f"<td>{r.get('fp_status', '?')}</td>"
            f"<td class='tc'>{_format_tags_html(r.get('existing_tags', {}))}</td></tr>"
            for r in failed_files
        )
        failed_section = f"""
<h2>Files That Could Not Be Fingerprinted ({len(failed_files)})</h2>
<table class="ft">
  <thead><tr><th>File</th><th>Reason</th><th>Current tags</th></tr></thead>
  <tbody>{failed_rows}</tbody>
</table>"""
    else:
        failed_section = ""

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MusicTagger Report — {report_time}</title>
<style>
  body{{font-family:Arial,sans-serif;margin:20px;color:#333;line-height:1.4}}
  h1{{color:#1a1a2e}}
  h2{{color:#16213e;margin-top:30px;border-bottom:2px solid #dee2e6;padding-bottom:6px}}
  .summary{{background:#f8f9fa;padding:15px 20px;border-radius:6px;margin-bottom:20px;display:inline-block}}
  .summary-table{{border-collapse:collapse;min-width:380px}}
  .summary-table td,.summary-table th{{padding:6px 14px;border:1px solid #ddd}}
  .summary-table th{{background:#e9ecef;text-align:left}}
  .ft{{border-collapse:collapse;width:100%;table-layout:fixed}}
  .ft th,.ft td{{border:1px solid #ccc;padding:7px 8px;vertical-align:top;word-wrap:break-word}}
  .ft th{{background:#343a40;color:#fff;text-align:left}}
  .ft tr:nth-child(even){{background:#f8f9fa}}
  .fn{{font-family:Consolas,monospace;font-size:11px;word-break:break-all}}
  .tc{{font-family:Consolas,monospace;font-size:11px}}
  .rs{{font-style:italic;font-size:12px}}
</style>
</head>
<body>
<h1>MusicTagger Report</h1>
<p>Generated: {report_time}</p>
{summary_html}
<h2>Problem Files</h2>
{problem_section}
{failed_section}
</body>
</html>"""

    with open("music_tagger_report.html", "w", encoding="utf-8") as f:
        f.write(html)


# ── Directory processing ────────────────────────────────────────────────────────

def process_directory(
    root_dir: str,
    acoustid_key: str,
    confidence_threshold: int,
    dry_run: bool,
    limit: int | None = None,
    eval_only: bool = False,
) -> None:
    claude_client = anthropic.Anthropic()

    all_files = [
        str(p)
        for p in Path(root_dir).rglob("*")
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not all_files:
        log.warning(f"No MP3 or FLAC files found in {root_dir}")
        return

    audio_files = all_files[:limit] if limit else all_files

    log.info(
        f"Found {len(all_files)} file(s); processing {len(audio_files)}"
        + (f" (--limit {limit})" if limit else "")
    )
    if dry_run:
        log.info("DRY RUN — no files will be modified")

    # ── Pass 1: fingerprint & evaluate ─────────────────────────────────────────
    log.info("\n" + "─" * 60)
    log.info("PASS 1: Fingerprinting & pre-evaluation")
    log.info("─" * 60)

    evaluations = []
    for i, filepath in enumerate(audio_files, 1):
        log.info(f"[{i}/{len(audio_files)}] Evaluating: {Path(filepath).name}")
        evaluation = evaluate_file_for_claude(filepath, acoustid_key)
        evaluations.append(evaluation)
        time.sleep(0.2)

    needs_claude_list = [e for e in evaluations if e["needs_claude"]]
    estimate = estimate_anthropic_cost(len(needs_claude_list))

    overall_counts = {}
    for e in evaluations:
        s = e["problems"]["overall_status"]
        overall_counts[s] = overall_counts.get(s, 0) + 1

    log.info("\n" + "═" * 60)
    log.info("PRE-RUN EVALUATION SUMMARY")
    log.info("═" * 60)
    log.info(f"  Total files scanned        : {len(evaluations)}")
    log.info(f"  ✓ Tags look correct        : {overall_counts.get('ok', 0)}")
    log.info(f"  ⚠ Need attention           : {overall_counts.get('needs_attention', 0)}")
    log.info(f"  ✗ AcoustID no match        : {overall_counts.get('unresolvable', 0)}")
    log.info(f"  ✗ Fingerprint failed       : {overall_counts.get('fingerprint_failed', 0)}")
    log.info(f"  Files requiring Claude     : {len(needs_claude_list)}")
    log.info(f"  Est. Claude API cost       : ${estimate:.4f}")
    log.info("═" * 60)

    if not needs_claude_list or eval_only:
        if eval_only:
            log.info("Eval-only mode — stopping after Pass 1.")
        else:
            log.info("No files require Claude — saving report.")
        results = [_evaluation_to_result(e) for e in evaluations]
        save_reports(results, evaluations, dry_run=dry_run)
        return

    if sys.stdin.isatty():
        answer = input(
            f"\nProceed to consult Claude for {len(needs_claude_list)} file(s)? [y/N]: "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            log.info("Aborted — saving pre-evaluation report.")
            results = [_evaluation_to_result(e) for e in evaluations]
            save_reports(results, evaluations, dry_run=dry_run)
            return

    # ── Pass 2: Claude verification ─────────────────────────────────────────────
    log.info("\n" + "─" * 60)
    log.info(f"PASS 2: Claude verification ({len(needs_claude_list)} file(s))")
    log.info("─" * 60)

    results = []
    for i, evaluation in enumerate(evaluations, 1):
        log.info(f"[{i}/{len(evaluations)}]")
        result = process_file(
            evaluation["file"],
            acoustid_key,
            claude_client,
            confidence_threshold,
            dry_run,
            precomputed=evaluation,
        )
        results.append(result)
        time.sleep(0.5)

    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    log.info("\n" + "═" * 60)
    log.info(f"FINAL SUMMARY  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    log.info("═" * 60)
    for status, count in sorted(status_counts.items()):
        log.info(f"  {status:<28}: {count}")
    log.info("═" * 60)

    save_reports(results, evaluations, dry_run=dry_run)


def _evaluation_to_result(e: dict) -> dict:
    """Convert a pre-evaluation record to a result record (used when Claude is not run)."""
    overall = e["problems"]["overall_status"]
    status = {
        "ok": "ok",
        "needs_attention": "skipped",
        "unresolvable": "unresolvable",
        "fingerprint_failed": "fingerprint_failed",
    }.get(overall, "ok")
    return {
        "file": e["file"],
        "status": status,
        "confidence": None,
        "reasoning": e["evaluation_reason"],
        "tags_written": {},
        "existing_tags": e["existing_tags"],
        "evaluation_reason": e["evaluation_reason"],
        "problems": e["problems"],
        "fp_status": e.get("fp_status", FP_OK),
    }


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Tag MP3/FLAC files using AcoustID + MusicBrainz + Claude AI"
    )
    parser.add_argument("directory", help="Root directory to scan")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing any tags"
    )
    parser.add_argument(
        "--confidence", type=int, default=DEFAULT_CONFIDENCE_THRESHOLD, metavar="N",
        help=f"Minimum Claude confidence to write tags (default: {DEFAULT_CONFIDENCE_THRESHOLD})"
    )
    parser.add_argument(
        "--acoustid-key", default=ACOUSTID_API_KEY,
        help="AcoustID API key (or set ACOUSTID_API_KEY env var)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process only the first N files (useful for testing)"
    )
    parser.add_argument(
        "--eval-only", action="store_true",
        help="Run fingerprinting/pre-evaluation only — show cost estimate and save report without calling Claude"
    )
    args = parser.parse_args()

    if not args.acoustid_key:
        print("Error: AcoustID API key required.")
        print("  Register free at https://acoustid.org/api-key")
        print("  Then set ACOUSTID_API_KEY in your environment or keys file.")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    if not Path(args.directory).is_dir():
        print(f"Error: {args.directory!r} is not a directory")
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
        limit=args.limit,
        eval_only=args.eval_only,
    )


if __name__ == "__main__":
    main()
