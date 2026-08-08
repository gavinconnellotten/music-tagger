"""Read-only scan for two player-category problems:
  A. NO-ARTIST albums  — every track lacks BOTH artist and album artist (Plex/MA
     show these under a blank/"no artist" heading).
  B. VARIOUS-ARTISTS   — album artist is literally 'Various Artists' and/or the
     compilation flag is set (these songs land in the Various Artists category).

Prints the folder path + context (album tag, sample titles, any stray artist) so
the right fix is obvious. Read-only.
"""
import sys
from collections import Counter
from pathlib import Path
from mutagen import File as MFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from music_tagger.matcher import cluster_albums  # noqa: E402

ROOT = Path(r"Z:\Music")


def get(af, key) -> str:
    try:
        v = af[key] if af and key in af else None
    except Exception:  # noqa: BLE001
        v = None
    return str(v[0] if isinstance(v, list) else v).strip() if v else ""


def comp_truthy(af) -> bool:
    if af is None or af.tags is None:
        return False
    t = af.tags
    if "compilation" in t:
        v = t["compilation"]
        val = str(v[0] if isinstance(v, list) else v).strip()
    elif hasattr(t, "getall") and t.getall("TCMP"):
        fr = t.getall("TCMP")[0]
        val = str(fr.text[0]).strip() if getattr(fr, "text", None) else ""
    else:
        return False
    return val not in ("", "0")


def main() -> None:
    by_album = cluster_albums(str(ROOT))
    n_files = sum(len(v) for v in by_album.values())
    print(f"Scanning {n_files} file(s) across {len(by_album)} album(s)...\n")

    no_artist, various = [], []
    for folder in sorted(by_album):
        files = by_album[folder]
        artists, aartists, albums, titles = Counter(), Counter(), Counter(), []
        comps = 0
        for p in files:
            easy = MFile(str(p), easy=True)          # one open for the text fields
            artists[get(easy, "artist")] += 1
            aartists[get(easy, "albumartist")] += 1
            albums[get(easy, "album")] += 1
            titles.append(get(easy, "title") or p.name)
            if comp_truthy(MFile(str(p))):           # one open for the compilation frame
                comps += 1
        nonempty_ar = {a for a in artists if a}
        nonempty_aa = {a for a in aartists if a}

        if not nonempty_ar and not nonempty_aa:
            no_artist.append((folder, len(files), [a for a in albums if a], titles[:3]))
        elif any("various" in a.lower() for a in nonempty_aa) or comps:
            reason = []
            if any("various" in a.lower() for a in nonempty_aa):
                reason.append("albumartist=Various Artists")
            if comps:
                reason.append(f"compilation flag on {comps}/{len(files)}")
            various.append((folder, len(files), nonempty_ar, nonempty_aa, reason))

    print("=" * 80)
    print(f"A. NO-ARTIST ALBUMS ({len(no_artist)})")
    print("=" * 80)
    for folder, n, albums, titles in no_artist:
        print(f"[{folder}]  ({n} files)")
        print(f"    album tag : {albums or '(blank)'}")
        print(f"    e.g.      : {titles}")
    print()
    print("=" * 80)
    print(f"B. VARIOUS-ARTISTS ALBUMS ({len(various)})")
    print("=" * 80)
    for folder, n, ar, aa, reason in various:
        print(f"[{folder.name}]  ({n} files)  — {'; '.join(reason)}")
        print(f"    track artists : {sorted(ar)[:6]}{' …' if len(ar) > 6 else ''}")
        print(f"    album artists : {sorted(aa)}")


if __name__ == "__main__":
    main()
