"""Tag read/write via mutagen, plus field normalization. Shared by matcher, writer, and rollback."""
import re
import logging
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TRCK, TDRC, TCON, TPE2, TPOS, TXXX,
)

log = logging.getLogger(__name__)

# Fields we track everywhere (read, compare, propose, write).
CHECKED_FIELDS = [
    "title", "artist", "album", "albumartist",
    "tracknumber", "date", "genre", "discnumber",
]

# Reserved (non-text) keys carrying MusicBrainz artist IDs as a LIST. Players like
# Music Assistant trust these IDs over the artist/albumartist text and re-expand
# collaborators from them, so when we reduce a credit we must reduce its ID list to
# match. Handled specially by read/write/restore/diff; deliberately NOT in
# CHECKED_FIELDS (values are lists, not strings). Each maps to (ID3 TXXX desc, FLAC key).
MB_ALBUMARTIST_ID = "musicbrainz_albumartistid"
MB_ARTIST_ID = "musicbrainz_artistid"
_MB_ID_FRAMES = {
    MB_ALBUMARTIST_ID: ("MusicBrainz Album Artist Id", "musicbrainz_albumartistid"),
    MB_ARTIST_ID: ("MusicBrainz Artist Id", "musicbrainz_artistid"),
}
# The text field each ID list belongs to (drives the diff label + change trigger).
_MB_ID_OWNER = {MB_ALBUMARTIST_ID: "albumartist", MB_ARTIST_ID: "artist"}

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
            for key, (desc, _) in _MB_ID_FRAMES.items():
                fr = audio.get(f"TXXX:{desc}")
                if fr is not None and list(fr.text):
                    tags[key] = list(fr.text)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in _FLAC_FIELDS:
                val = audio.get(key)
                if val:
                    tags[key] = val[0]
            for key, (_, fkey) in _MB_ID_FRAMES.items():
                vals = audio.get(fkey)
                if vals:
                    tags[key] = list(vals)
    except Exception as e:  # noqa: BLE001 - reading must never crash a run
        log.debug(f"Could not read tags from {filepath}: {e}")
    return tags


def _apply_mb_ids(audio, ext: str, source: dict) -> None:
    """Write/clear the MusicBrainz-ID frames present (as keys) in `source`.
    A present key with a non-empty list sets the frame; an empty list clears it."""
    for key, (desc, fkey) in _MB_ID_FRAMES.items():
        if key not in source:
            continue
        ids = [i for i in (source.get(key) or []) if i]
        if ext == ".mp3":
            if ids:
                audio[f"TXXX:{desc}"] = TXXX(encoding=3, desc=desc, text=ids)
            else:
                audio.delall(f"TXXX:{desc}")
        else:
            if ids:
                audio[fkey] = ids
            elif fkey in audio:
                del audio[fkey]


def write_tags(filepath: str, tags: dict) -> bool:
    """Write the given {field: value} tags to the file. Returns True on success.

    A `MB_ALBUMARTIST_ID` / `MB_ARTIST_ID` entry (a list of IDs) is written to the
    matching MusicBrainz-ID frame; an empty list clears it.
    """
    ext = Path(filepath).suffix.lower()
    clean = {k: str(v) for k, v in tags.items()
             if k not in _MB_ID_FRAMES and v not in (None, "")}
    try:
        if ext == ".mp3":
            try:
                audio = ID3(filepath)
            except ID3NoHeaderError:
                audio = ID3()
            for key, Frame in _ID3_WRITE.items():
                if clean.get(key):
                    audio[Frame.__name__] = Frame(encoding=3, text=clean[key])
            _apply_mb_ids(audio, ext, tags)
            audio.save(filepath)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for key in _FLAC_FIELDS:
                if clean.get(key):
                    audio[key] = clean[key]
            _apply_mb_ids(audio, ext, tags)
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
        # MusicBrainz-ID frames are restored only if the snapshot recorded the key
        # (snapshots from runs predating these keys won't have them — leave those
        # frames untouched rather than wrongly deleting them). _apply_mb_ids keys
        # off presence in `original`, giving exactly that behaviour.
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
            _apply_mb_ids(audio, ext, original)
            audio.save(filepath)
        elif ext == ".flac":
            audio = FLAC(filepath)
            for field in _FLAC_FIELDS:
                if original.get(field):
                    audio[field] = str(original[field])
                elif field in audio:
                    del audio[field]
            _apply_mb_ids(audio, ext, original)
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


def album_artist_id_for(name: str, names: list, ids: list) -> str | None:
    """Return the MusicBrainz ID aligned (by position) with `name` in the parallel
    (names, ids) credit lists from the matched release, or None if not found.
    Lets us keep exactly the kept artist's ID when reducing a joint credit."""
    if not name or not ids:
        return None
    target = _normalize(name)
    for n, i in zip(names, ids):
        if _normalize(n) == target:
            return i
    return None


# Joins that separate distinct artists within one credit string.
_SEP_RE = re.compile(r"\s+&\s+|\s+and\s+|\s+with\s+|\s*/\s*|\s*,\s*", re.IGNORECASE)


def reduce_credit(text: str, context: str, names, ids) -> tuple:
    """Reduce a multi-artist credit to the library's primary artist.

    Returns (new_text, new_ids) where new_ids is the MusicBrainz-ID list to WRITE
    ([kept_id], or [] to clear), or None meaning "leave the ID frame untouched".

    `context` is the artist folder the album is filed under — the library's
    statement of who is primary. The decision is folder-PREFIX based, which encodes
    that filing intent and distinguishes the two hard cases that look identical
    otherwise (both are single MusicBrainz entities containing "&"):
      - "Elvis Costello & The Attractions" filed under "Elvis Costello …"  -> flatten
        to "Elvis Costello" (folder is named after one part, the frontman);
      - "Simon & Garfunkel" filed under "Simon & Garfunkel …"             -> keep
        (folder is named after the WHOLE credit), and also kept under a "Paul Simon"
        folder (which does not start with "Simon"), avoiding a spurious "Simon".

    `names`/`ids` are beets' parallel artist-ENTITY lists (info.artists/…_ids); when
    present (a true multi-entity collaboration) the kept entity's ID is preserved.
    For a single-entity band phrase we flatten the text but leave the ID frame alone.

    Rules in order: strip "feat./ft./featuring …"; ";"-joined (MusicBrainz "primary
    first", e.g. classical "Composer; Performer") -> first name (keeps the composer);
    else folder-prefix reduction. Soundtracks/various-artists (folder matches nothing)
    are left intact."""
    if not text:
        return text, None
    text = primary_artist(text)
    if ";" in text:
        first = text.split(";")[0].strip() or text
        if first != text:
            kept = album_artist_id_for(first, names or [], ids or [])
            return first, ([kept] if kept else [])
        return text, None
    ctx = _normalize(context)
    names, ids = list(names or []), list(ids or [])
    # Multi-entity collaboration: keep the entity the folder is named after (+ its ID).
    if len(names) >= 2:
        matched = [k for k, n in enumerate(names) if _normalize(n) and ctx.startswith(_normalize(n))]
        if len(matched) == 1:
            k = matched[0]
            kept_id = ids[k] if k < len(ids) else None
            return names[k], ([kept_id] if kept_id else [])
        return text, None
    # Single entity (band phrase) or no entity data: keep if the folder is named after
    # the whole credit; else flatten to the one part the folder is named after.
    if not text or ctx.startswith(_normalize(text)):
        return text, None
    parts = [p.strip() for p in _SEP_RE.split(text) if p.strip()]
    if len(parts) >= 2:
        matched = [p for p in parts if _normalize(p) and ctx.startswith(_normalize(p))]
        if len(matched) == 1:
            return matched[0], None
    return text, None


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
    # MusicBrainz artist-ID lists — only when the proposal specifies them.
    for key, owner in _MB_ID_OWNER.items():
        if key in proposed and list(proposed.get(key) or []) != list(current.get(key) or []):
            changed.append(f"{owner}_id")
    return changed
