"""Tag read/write via mutagen, plus field normalization. Shared by matcher, writer, and rollback."""
import re
import logging
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TRCK, TDRC, TCON, TPE2, TPOS,
)

log = logging.getLogger(__name__)

# Fields we track everywhere (read, compare, propose, write).
CHECKED_FIELDS = [
    "title", "artist", "album", "albumartist",
    "tracknumber", "date", "genre", "discnumber",
]

_ID3_READ = {
    "TIT2": "title", "TPE1": "artist", "TALB": "album", "TPE2": "albumartist",
    "TRCK": "tracknumber", "TDRC": "date", "TCON": "genre", "TPOS": "discnumber",
}
_ID3_WRITE = {
    "title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2,
    "tracknumber": TRCK, "date": TDRC, "genre": TCON, "discnumber": TPOS,
}
_FLAC_FIELDS = ["title", "artist", "album", "albumartist",
                "tracknumber", "discnumber", "date", "genre"]


def read_existing_tags(filepath: str) -> dict:
    """Return current tags as a {field: str} dict (missing fields omitted)."""
    ext = Path(filepath).suffix.lower()
    tags: dict = {}
    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                return tags
            for frame, key in _ID3_READ.items():
                val = audio.get(frame)
                if val:
                    tags[key] = str(val)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in _FLAC_FIELDS:
                val = audio.get(key)
                if val:
                    tags[key] = val[0]
    except Exception as e:  # noqa: BLE001 - reading must never crash a run
        log.debug(f"Could not read tags from {filepath}: {e}")
    return tags


def write_tags(filepath: str, tags: dict) -> bool:
    """Write the given {field: value} tags to the file. Returns True on success."""
    ext = Path(filepath).suffix.lower()
    clean = {k: str(v) for k, v in tags.items() if v not in (None, "")}
    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                audio = ID3()
            for key, Frame in _ID3_WRITE.items():
                if clean.get(key):
                    audio[Frame.__name__] = Frame(encoding=3, text=clean[key])
            audio.save(filepath)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in _FLAC_FIELDS:
                if clean.get(key):
                    audio[key] = clean[key]
            audio.save()
        else:
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.error(f"Failed to write tags to {filepath}: {e}")
        return False


def restore_tags(filepath: str, original: dict) -> bool:
    """Set the CHECKED_FIELDS to exactly `original`: rewrite present fields,
    and DELETE fields absent from the snapshot (i.e. ones a run added). This is
    what makes --undo a true rollback rather than a partial overwrite."""
    ext = Path(filepath).suffix.lower()
    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                audio = ID3()
            for field, Frame in _ID3_WRITE.items():
                frame_name = Frame.__name__
                if original.get(field):
                    audio[frame_name] = Frame(encoding=3, text=str(original[field]))
                else:
                    audio.delall(frame_name)
            audio.save(filepath)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for field in _FLAC_FIELDS:
                if original.get(field):
                    audio[field] = str(original[field])
                elif field in audio:
                    del audio[field]
            audio.save()
        else:
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.error(f"Failed to restore tags on {filepath}: {e}")
        return False


def _normalize(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    # tracknumber/discnumber often stored as "3/12" — compare on the leading number
    text = text.split("/")[0]
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _year(value) -> str:
    m = re.search(r"\d{4}", str(value or ""))
    return m.group(0) if m else ""


# A trailing "feat./ft./featuring <guest>" credit on an *album* artist. Only these
# explicit guest markers are stripped — NOT "&" / "/" / "with", which often denote
# genuine duos or aliases (e.g. "MF DOOM & MF Grimm", "Viktor Vaughn / MF DOOM").
_FEATURING_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def primary_artist(value: str) -> str:
    """Strip a trailing 'feat./ft./featuring …' guest credit, leaving the primary
    artist. Keeps album-artist tags from spawning guest entries in players that
    split albumartist on featuring credits (e.g. Music Assistant)."""
    if not value:
        return value
    return _FEATURING_RE.sub("", value).strip()


# Joins that denote multiple distinct artists in one albumartist string. ";" is
# deliberately EXCLUDED: it's the proper multi-value separator (e.g. classical
# "Composer; Performer") and must be left intact.
_MULTIARTIST_RE = re.compile(r"\s+&\s+|\s+and\s+|\s+with\s+|\s*/\s*|\s*,\s*", re.IGNORECASE)


def primary_from_context(albumartist: str, context: str) -> str:
    """Reduce a multi-artist albumartist to the single credited name that matches
    `context` — the name of the artist folder the album is filed under, i.e. the
    artist the *library* treats as primary. This steers joint credits like
    "Elvis Costello & Allen Toussaint" (filed under Elvis Costello) down to the
    lead, so players that split albumartist don't list the collaborator separately.

    Returns the value UNCHANGED when we can't be confident:
      - not a multi-artist credit, or it uses ';' (proper multi-value), or
      - zero credited names match the folder (e.g. a soundtrack or a mistag), or
      - several match (e.g. "Simon & Garfunkel" under a "Simon & Garfunkel" folder —
        a band name, not a collaboration).
    Word order is irrelevant: folder context, not position, picks the lead."""
    if not albumartist or ";" in albumartist:
        return albumartist
    names = [n.strip() for n in _MULTIARTIST_RE.split(albumartist) if n.strip()]
    if len(names) < 2:
        return albumartist
    ctx = _normalize(context)
    matched = [n for n in names if _normalize(n) and _normalize(n) in ctx]
    return matched[0] if len(matched) == 1 else albumartist


def diff_tags(current: dict, proposed: dict) -> list[str]:
    """Fields where the proposal is non-empty and meaningfully differs from current.

    `date` is compared on the year only — a precision difference like
    "1992-03" vs "1992" is not a real change and must not trigger a rewrite.
    """
    changed = []
    for field in CHECKED_FIELDS:
        prop = proposed.get(field)
        if prop in (None, ""):
            continue
        if field == "date":
            if _year(current.get(field)) != _year(prop):
                changed.append(field)
        elif _normalize(current.get(field)) != _normalize(prop):
            changed.append(field)
    return changed
