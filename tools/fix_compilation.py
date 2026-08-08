"""Normalize a bogus iTunes 'compilation' / ID3 'TCMP' flag across the library.

PMEDIA rips stamp the literal string "PMEDIA" into the compilation field; Plex
reads that truthy value as "Various Artists compilation" and pulls the album out
of the artist's discography (Music Assistant ignores it). A legitimate flag is
"1" (VA) or absent/"0".

Per album, decide by who the artists are:
  - genuine various-artists album  -> set compilation = "1"  (correct VA flag)
  - single-artist album            -> remove the flag entirely
Only files whose current value is bogus (not in "", "0", "1") are touched, so a
correct "1" on a real VA album is left alone. Dry-run by default.

Usage:
  python tools/fix_compilation.py "50 Years"            # dry-run, one album
  python tools/fix_compilation.py "50 Years" --apply    # write
  python tools/fix_compilation.py --all                 # dry-run, whole library
  python tools/fix_compilation.py --all --apply         # write, whole library
"""
import sys
from pathlib import Path
from mutagen import File as MFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, TCMP

ROOT = Path(r"Z:\Music")
GOOD = {"", "0", "1"}  # legitimate compilation values; anything else is garbage


def comp_value(af) -> str | None:
    """The file's compilation/TCMP value as a string, or None if the frame is absent."""
    if af is None or af.tags is None:
        return None
    t = af.tags
    if "compilation" in t:                       # FLAC / Vorbis
        v = t["compilation"]
        return str(v[0] if isinstance(v, list) else v)
    if t.getall("TCMP") if hasattr(t, "getall") else ("TCMP" in t):  # ID3 (MP3)
        fr = t.getall("TCMP")[0] if hasattr(t, "getall") else t["TCMP"]
        return str(fr.text[0]) if getattr(fr, "text", None) else ""
    return None


def _easy(af, key) -> str:
    try:
        v = af[key] if af and key in af else None
    except Exception:  # noqa: BLE001
        v = None
    if not v:
        return ""
    return str(v[0] if isinstance(v, list) else v).strip()


def album_is_va(files: list[Path]) -> bool:
    """Heuristic: the album is a genuine various-artists compilation if its album
    artist says 'Various Artists', or it has no album artist but multiple distinct
    track artists. Single-artist albums (incl. ones with feat. guests under one
    albumartist) are NOT VA."""
    albumartists, artists = set(), set()
    for p in files:
        af = MFile(str(p), easy=True)
        if af is None:
            continue
        aa = _easy(af, "albumartist")
        ar = _easy(af, "artist")
        if aa:
            albumartists.add(aa)
        if ar:
            artists.add(ar)
    if any("various" in a.lower() for a in albumartists):
        return True
    if not albumartists and len(artists) > 1:
        return True
    return False


def write_flag(path: Path, target: str) -> bool:
    """target='' removes the flag; target='1' sets it. Returns True if written."""
    suffix = path.suffix.lower()
    if suffix == ".flac":
        af = FLAC(str(path))
        if target:
            af["compilation"] = target
        elif "compilation" in af:
            del af["compilation"]
        else:
            return False
        af.save()
        return True
    if suffix == ".mp3":
        try:
            id3 = ID3(str(path))
        except ID3NoHeaderError:
            return False
        if target:
            id3.setall("TCMP", [TCMP(encoding=3, text=[target])])
        elif id3.getall("TCMP"):
            id3.delall("TCMP")
        else:
            return False
        id3.save()
        return True
    return False


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args
    do_all = "--all" in args
    needles = [a for a in args if not a.startswith("--")]

    exts = (".flac", ".mp3")
    all_files = [p for p in ROOT.rglob("*") if p.suffix.lower() in exts]
    if do_all:
        files = all_files
    elif needles:
        nl = needles[0].lower()
        files = [p for p in all_files if nl in str(p.relative_to(ROOT)).lower()]
    else:
        print("give an album substring or --all"); sys.exit(1)

    # Group by the top-level album folder under ROOT (so disc subfolders fold in).
    by_album: dict[Path, list[Path]] = {}
    for p in files:
        by_album.setdefault(ROOT / p.relative_to(ROOT).parts[0], []).append(p)

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] scanning {len(files)} file(s) in {len(by_album)} album(s)...")
    total_bad = removed_albums = va_albums = written = 0
    for folder in sorted(by_album):
        afiles = by_album[folder]
        bad = [(p, comp_value(MFile(str(p)))) for p in afiles]
        bad = [(p, v) for p, v in bad if v is not None and v not in GOOD]
        if not bad:
            continue
        total_bad += len(bad)
        va = album_is_va(afiles)
        target = "1" if va else ""
        action = "SET compilation=1 (VA)" if va else "REMOVE flag (single-artist)"
        va_albums += 1 if va else 0
        removed_albums += 0 if va else 1
        vals = sorted({v for _, v in bad})
        print(f"  {folder.name}: {len(bad)}/{len(afiles)} bogus {vals} -> {action}")
        if apply:
            n = sum(1 for p, _ in bad if write_flag(p, target))
            written += n
            print(f"      wrote {n} file(s)")
    print(f"[{mode}] {total_bad} bogus file(s); "
          f"{removed_albums} single-artist album(s) -> remove, "
          f"{va_albums} VA album(s) -> set 1"
          + (f"; wrote {written} file(s)" if apply else "  — re-run with --apply"))


if __name__ == "__main__":
    main()
