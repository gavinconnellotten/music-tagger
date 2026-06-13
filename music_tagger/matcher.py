"""beets-powered album matching.

Clusters files by folder, runs beets' MusicBrainz autotagger over each album,
and returns a JSON-serializable proposal (recommendation + ranked candidates,
each with per-file proposed tags). No library DB; no files are moved or written.
"""
import os
import re
import time
import hashlib
import logging
from pathlib import Path

from .tags import read_existing_tags, CHECKED_FIELDS

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".mp3", ".flac"}
_MAX_CANDIDATES = 5
_beets_ready = False


def init_beets(fingerprint: bool = False) -> None:
    """Load beets metadata-source plugins once (beets 2.x requirement).

    fingerprint=True also loads `chroma` (AcoustID), enabling fingerprint matching.
    NOTE: beets' load_plugins() is one-shot, so the fingerprint choice is fixed for
    the process — pick it at startup.
    """
    global _beets_ready
    if _beets_ready:
        return
    from beets import config, plugins
    plugin_list = ["musicbrainz"]
    if fingerprint:
        plugin_list.append("chroma")
        # fpcalc ships in the venv Scripts dir — make sure chroma can find it.
        scripts = Path(__file__).resolve().parent.parent / ".venv" / "Scripts"
        if scripts.exists():
            os.environ["PATH"] = str(scripts) + os.pathsep + os.environ.get("PATH", "")
        key = os.environ.get("ACOUSTID_API_KEY", "")
        if key:
            config["acoustid"]["apikey"] = key
    config["plugins"] = plugin_list
    plugins.load_plugins()
    # Quiet beets' own logging so our run output stays readable.
    logging.getLogger("beets").setLevel(logging.WARNING)
    _beets_ready = True


# Disc subfolders like "CD1", "CD 2", "Disc 3", "Disk1" — merged into the parent
# album so a multi-disc release is matched as one release, not per-disc fragments.
_DISC_RE = re.compile(r"^(cd|disc|disk|disco)\s*0*\d+$", re.IGNORECASE)


def _album_root(folder: Path) -> Path:
    return folder.parent if _DISC_RE.match(folder.name) else folder


def cluster_albums(root: str) -> dict[Path, list[Path]]:
    """Group supported audio files by album. Files in CD1/CD2/Disc-N subfolders are
    merged into their parent so multi-disc releases cluster as a single album."""
    by_dir: dict[Path, list[Path]] = {}
    for p in Path(root).rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            by_dir.setdefault(_album_root(p.parent), []).append(p)
    return {d: sorted(files) for d, files in sorted(by_dir.items())}


def album_key(files: list[Path]) -> str:
    """Stable hash of the album's file set + sizes + mtimes — cache invalidates on change."""
    h = hashlib.sha1()
    for p in sorted(files):
        try:
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}|".encode("utf-8", "replace"))
        except OSError:
            h.update(f"{p.name}|?|?|".encode("utf-8", "replace"))
    return h.hexdigest()


def _g(obj, name):
    return getattr(obj, name, None)


def _date_str(info) -> str | None:
    """Assemble the fullest date beets offers (YYYY[-MM[-DD]]) — don't drop precision."""
    y = _g(info, "year") or _g(info, "original_year")
    if not y:
        return None
    m = _g(info, "month") or _g(info, "original_month")
    d = _g(info, "day") or _g(info, "original_day")
    s = f"{int(y):04d}"
    if m:
        s += f"-{int(m):02d}"
        if d:
            s += f"-{int(d):02d}"
    return s


def _trackinfo_to_tags(ti, info) -> dict:
    """Map a beets TrackInfo (+ its AlbumInfo) onto our CHECKED_FIELDS, defensively."""
    tracknum = _g(ti, "medium_index") or _g(ti, "index")
    disc = _g(ti, "medium")
    mediums = _g(info, "mediums") or 1
    return {
        "title": _g(ti, "title"),
        "artist": _g(ti, "artist") or _g(info, "artist"),
        "album": _g(info, "album"),
        "albumartist": _g(info, "artist"),
        "tracknumber": str(tracknum) if tracknum else None,
        "date": _date_str(info),
        "genre": _g(info, "genre"),
        "discnumber": str(disc) if (disc and mediums > 1) else None,
    }


def _read_item(path: Path, attempts: int = 3, delay: float = 0.5):
    """Read one file into a beets Item, retrying transient failures (flaky network drive)."""
    from beets.library import Item
    last = None
    for i in range(attempts):
        try:
            return Item.from_path(str(path))
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last


def match_album(files: list[Path]) -> dict:
    """Run the autotagger over one album's files. Returns a serializable proposal dict.

    Files that can't be read (corrupt, or a transient network blip after retries) are
    skipped rather than failing the whole album; they're reported in `unreadable`.
    """
    from beets.autotag import match

    current = {str(p): read_existing_tags(str(p)) for p in files}

    items, unreadable = [], []
    for p in files:
        try:
            items.append(_read_item(p))
        except Exception as e:  # noqa: BLE001
            log.warning(f"Unreadable, skipping: {p.name} :: {e!r}")
            unreadable.append(str(p))

    if not items:
        return {"current": current, "cur_artist": "", "cur_album": "",
                "recommendation": "error", "error": "all files unreadable",
                "unreadable": unreadable, "candidates": []}

    try:
        cur_artist, cur_album, proposal = match.tag_album(items)
    except Exception as e:  # noqa: BLE001 - network / parse failures shouldn't kill the run
        log.warning(f"tag_album failed for {files[0].parent}: {e!r}")
        return {"current": current, "cur_artist": "", "cur_album": "",
                "recommendation": "error", "error": repr(e),
                "unreadable": unreadable, "candidates": []}

    # Map beets Item.path (bytes) back to the original file path string we keyed `current` on.
    def item_path(it) -> str:
        raw = it.path.decode("utf-8", "replace") if isinstance(it.path, bytes) else str(it.path)
        return str(Path(raw))

    candidates = []
    for m in proposal.candidates[:_MAX_CANDIDATES]:
        per_file = {}
        per_file_credits = {}
        for it, ti in m.mapping.items():
            path = item_path(it)
            per_file[path] = _trackinfo_to_tags(ti, m.info)
            # Parallel per-track artist names + MusicBrainz IDs (same order), so a
            # reduced `artist` can keep exactly the matching ID.
            per_file_credits[path] = {
                "artist_names": list(_g(ti, "artists") or []),
                "artist_ids": list(_g(ti, "artists_ids") or []),
            }
        candidates.append({
            "distance": round(float(m.distance), 4),
            "album": _g(m.info, "album"),
            "albumartist": _g(m.info, "artist"),
            # Parallel album-artist credit names + MusicBrainz IDs (same order),
            # so a reduced albumartist can keep exactly the matching ID.
            "albumartists": list(_g(m.info, "artists") or []),
            "albumartist_ids": list(_g(m.info, "artists_ids") or []),
            "year": _g(m.info, "year"),
            "album_id": _g(m.info, "album_id"),
            "country": _g(m.info, "country"),
            "media": _g(m.info, "media"),
            "num_matched": len(m.mapping),
            "extra_items": len(m.extra_items),
            "extra_tracks": len(m.extra_tracks),
            "per_file": per_file,
            "per_file_credits": per_file_credits,
        })

    return {
        "current": current,
        "cur_artist": cur_artist or "",
        "cur_album": cur_album or "",
        "recommendation": proposal.recommendation.name,  # none | low | medium | strong
        "unreadable": unreadable,
        "candidates": candidates,
    }


def candidates_hash(proposal: dict) -> str:
    """Identity of the candidate set — Claude cache misses if candidates change."""
    h = hashlib.sha1()
    h.update(proposal.get("recommendation", "").encode())
    for c in proposal.get("candidates", []):
        h.update(f"|{c.get('album_id')}:{c.get('distance')}".encode("utf-8", "replace"))
    return h.hexdigest()
