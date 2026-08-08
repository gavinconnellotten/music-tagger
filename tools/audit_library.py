"""Read-only library audit: flag albums that will read as 'Various' or mislabelled
in a player (Plex especially). One network pass; writes music_tagger_audit.txt.

Albums are clustered with the tagger's own matcher.cluster_albums (by leaf folder,
disc subfolders folded in) so an artist 'Discography' parent doesn't masquerade as
one giant album. Per album it checks:
  - bogus compilation flag still present (non "", "0", "1")
  - compilation = "1" on a single-artist album  -> Plex files it under Various Artists
  - albumartist labelled 'Various Artists' but every track is one artist
  - inconsistent albumartist across tracks       -> album splits
  - albumartist missing on some/all tracks        -> Plex falls back to track artist
  - inconsistent album name across tracks         -> album splits

Usage: python tools/audit_library.py
"""
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from mutagen import File as MFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from music_tagger.matcher import cluster_albums  # noqa: E402

ROOT = Path(r"Z:\Music")
GOOD_COMP = {"", "0", "1"}
EXTS = (".flac", ".mp3")


def get(af, key) -> str:
    try:
        v = af[key] if af and key in af else None
    except Exception:  # noqa: BLE001
        v = None
    if not v:
        return ""
    return str(v[0] if isinstance(v, list) else v).strip()


def comp_value(af) -> str:
    """compilation/TCMP as a string ('' if absent)."""
    if af is None or af.tags is None:
        return ""
    t = af.tags
    if "compilation" in t:
        v = t["compilation"]
        return str(v[0] if isinstance(v, list) else v).strip()
    if hasattr(t, "getall") and t.getall("TCMP"):
        fr = t.getall("TCMP")[0]
        return str(fr.text[0]).strip() if getattr(fr, "text", None) else ""
    return ""


def main() -> None:
    by_album = cluster_albums(str(ROOT))  # leaf-folder clustering, disc folders folded in
    n_files = sum(len(v) for v in by_album.values())

    print(f"Auditing {n_files} file(s) across {len(by_album)} album(s)...")
    report = [f"# MusicTagger library audit — {datetime.now():%Y-%m-%d %H:%M:%S}",
              f"# {n_files} files / {len(by_album)} albums", ""]
    flagged = 0
    for folder in sorted(by_album):
        afiles = by_album[folder]
        n = len(afiles)
        albums, albumartists, artists, comps = Counter(), Counter(), Counter(), Counter()
        missing_aa = 0
        for p in afiles:
            af = MFile(str(p), easy=True)
            raw = MFile(str(p))
            alb, aa, ar = get(af, "album"), get(af, "albumartist"), get(af, "artist")
            if alb:
                albums[alb] += 1
            if aa:
                albumartists[aa] += 1
            else:
                missing_aa += 1
            if ar:
                artists[ar] += 1
            comps[comp_value(raw)] += 1

        single_artist = len(artists) <= 1
        is_va_label = any("various" in a.lower() for a in albumartists)
        problems = []

        bogus = sorted(v for v in comps if v and v not in GOOD_COMP)
        if bogus:
            problems.append(f"bogus compilation flag {bogus}")
        if comps.get("1") and single_artist and not is_va_label:
            only = next(iter(artists), "?")
            problems.append(f"compilation=1 on single-artist album ({only!r}) — Plex → Various Artists")
        if is_va_label and single_artist:
            only = next(iter(artists), "?")
            problems.append(f"albumartist 'Various Artists' but all tracks are {only!r}")
        if len(albumartists) > 1:
            problems.append(f"inconsistent albumartist: {dict(albumartists)}")
        if 0 < missing_aa < n:
            problems.append(f"albumartist missing on {missing_aa}/{n} tracks")
        elif missing_aa == n and len(artists) > 1:
            problems.append(f"no albumartist + {len(artists)} distinct artists — will split per-artist")
        if len(albums) > 1:
            problems.append(f"inconsistent album name: {dict(albums)}")

        if problems:
            flagged += 1
            block = [f"[{folder.name}]  ({n} files)"] + [f"    - {pr}" for pr in problems]
            print("\n".join(block))
            report.extend(block + [""])

    out = Path("music_tagger_audit.txt")
    summary = f"{flagged} album(s) flagged of {len(by_album)}"
    report.insert(2, f"# {summary}")
    out.write_text("\n".join(report), encoding="utf-8")
    print("=" * 70)
    print(summary)
    print(f"Full report: {out.resolve()}")


if __name__ == "__main__":
    main()
