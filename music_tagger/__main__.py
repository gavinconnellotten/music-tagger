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
import json
import time
import logging
import argparse
from pathlib import Path
from typing import Optional
from datetime import datetime

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
SUPPORTED_EXTENSIONS = {".mp3", ".flac"}

# Files below this Claude confidence score are skipped and logged for review
DEFAULT_CONFIDENCE_THRESHOLD = 75

# MusicBrainz requires a user-agent
musicbrainzngs.set_useragent("MusicTaggerAI", "1.0", "music-tagger@local")

# ── Logging ───────────────────────────────────────────────────────────────────

log_path = Path("music_tagger.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── AcoustID / MusicBrainz ────────────────────────────────────────────────────

def get_acoustid_candidates(filepath: str, api_key: str) -> list[dict]:
    """
    Fingerprint a file and return MusicBrainz recording candidates.
    Each candidate has a score (0–1) and a list of possible recordings.
    """
    if not api_key:
        raise ValueError("ACOUSTID_API_KEY is not set")

    candidates = []
    try:
        results = acoustid.match(
            api_key,
            filepath,
            meta="recordings releases releasegroups",
            parse=True,
        )
        for score, recording_id, title, artist in results:
            candidates.append({
                "acoustid_score": round(score, 3),
                "recording_id": recording_id,
                "title": title or "",
                "artist": artist or "",
            })
    except acoustid.NoBackendError:
        log.error("fpcalc not found — install Chromaprint and ensure fpcalc is on PATH")
    except acoustid.FingerprintGenerationError as e:
        log.warning(f"Fingerprint failed for {filepath}: {e}")
    except acoustid.WebServiceError as e:
        log.warning(f"AcoustID web service error for {filepath}: {e}")

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
            includes=["artists", "releases", "release-groups"],
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
) -> dict:
    """Process a single audio file. Returns a result summary dict."""
    result = {
        "file": filepath,
        "status": "unknown",
        "confidence": 0,
        "reasoning": "",
        "tags_written": {},
    }

    log.info(f"Processing: {Path(filepath).name}")

    # 1. Read existing tags
    existing_tags = read_existing_tags(filepath)

    # 2. Fingerprint and get AcoustID candidates
    raw_candidates = get_acoustid_candidates(filepath, acoustid_key)
    log.info(f"  AcoustID: {len(raw_candidates)} candidate(s)")

    # 3. Enrich top candidates with full MusicBrainz data (cap at 3 to save API calls)
    enriched_candidates = []
    for c in raw_candidates[:3]:
        mb_data = enrich_from_musicbrainz(c["recording_id"]) if c["recording_id"] else {}
        enriched_candidates.append({**c, **mb_data})

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

    results = []
    for filepath in audio_files:
        result = process_file(
            filepath, acoustid_key, claude_client, confidence_threshold, dry_run
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

    # Save JSON report
    report_path = Path("music_tagger_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    log.info(f"\nFull report saved to: {report_path}")


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

    process_directory(
        root_dir=args.directory,
        acoustid_key=args.acoustid_key,
        confidence_threshold=args.confidence,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()