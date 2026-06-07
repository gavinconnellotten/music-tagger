"""beets-powered album matching.

Clusters files by folder, runs beets' MusicBrainz autotagger over each album,
and returns a JSON-serializable proposal (recommendation + ranked candidates,
each with per-file proposed tags). No library DB; no files are moved or written.
"""
import hashlib
import logging
from pathlib import Path

from .tags import read_existing_tags, CHECKED_FIELDS

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".mp3", ".flac"}
_MAX_CANDIDATES = 5
_beets_ready = False


def init_beets() -> None:
    """Load the MusicBrainz metadata-source plugin once (beets 2.x requirement)."""
    global _beets_ready
    if _beets_ready:
        return
    from beets import config, plugins
    config["plugins"] = ["musicbrainz"]
    plugins.load_plugins()
    # Quiet beets' own logging so our run output stays readable.
    logging.getLogger("beets").setLevel(logging.WARNING)
    _beets_ready = True


def cluster_albums(root: str) -> dict[Path, list[Path]]:
    """Group supported audio files by their containing folder (= album)."""
    by_dir: dict[Path, list[Path]] = {}
    for p in Path(root).rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            by_dir.setdefault(p.parent, []).append(p)
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


def match_album(files: list[Path]) -> dict:
    """Run the autotagger over one album's files. Returns a serializable proposal dict."""
    from beets.library import Item
    from beets.autotag import match

    current = {str(p): read_existing_tags(str(p)) for p in files}

    try:
        items = [Item.from_path(str(p)) for p in files]
        cur_artist, cur_album, proposal = match.tag_album(items)
    except Exception as e:  # noqa: BLE001 - network / parse failures shouldn't kill the run
        log.warning(f"tag_album failed for {files[0].parent}: {e!r}")
        return {
            "current": current,
            "cur_artist": "",
            "cur_album": "",
            "recommendation": "error",
            "error": repr(e),
            "candidates": [],
        }

    # Map beets Item.path (bytes) back to the original file path string we keyed `current` on.
    def item_path(it) -> str:
        raw = it.path.decode("utf-8", "replace") if isinstance(it.path, bytes) else str(it.path)
        return str(Path(raw))

    candidates = []
    for m in proposal.candidates[:_MAX_CANDIDATES]:
        per_file = {}
        for it, ti in m.mapping.items():
            per_file[item_path(it)] = _trackinfo_to_tags(ti, m.info)
        candidates.append({
            "distance": round(float(m.distance), 4),
            "album": _g(m.info, "album"),
            "albumartist": _g(m.info, "artist"),
            "year": _g(m.info, "year"),
            "album_id": _g(m.info, "album_id"),
            "country": _g(m.info, "country"),
            "media": _g(m.info, "media"),
            "num_matched": len(m.mapping),
            "extra_items": len(m.extra_items),
            "extra_tracks": len(m.extra_tracks),
            "per_file": per_file,
        })

    return {
        "current": current,
        "cur_artist": cur_artist or "",
        "cur_album": cur_album or "",
        "recommendation": proposal.recommendation.name,  # none | low | medium | strong
        "candidates": candidates,
    }


def candidates_hash(proposal: dict) -> str:
    """Identity of the candidate set — Claude cache misses if candidates change."""
    h = hashlib.sha1()
    h.update(proposal.get("recommendation", "").encode())
    for c in proposal.get("candidates", []):
        h.update(f"|{c.get('album_id')}:{c.get('distance')}".encode("utf-8", "replace"))
    return h.hexdigest()
