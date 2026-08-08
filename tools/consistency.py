"""Ad-hoc: read live on-disk tags for an album folder and flag inconsistencies that
make a player split/misorder it (mixed album/albumartist/date/genre, missing or
duplicate disc/track numbers)."""
import sys
from collections import Counter
from pathlib import Path
from mutagen import File as MFile

root = Path(r"Z:\Music")
needle = sys.argv[1].lower()
folders = sorted([p for p in root.iterdir() if p.is_dir() and needle in p.name.lower()])
if not folders:
    print("no match for", needle); sys.exit(0)

FIELDS = ["album", "albumartist", "artist", "date", "year", "originaldate",
          "genre", "discnumber", "disctotal", "tracknumber"]

for folder in folders:
    files = sorted([p for p in folder.rglob("*") if p.suffix.lower() in (".flac", ".mp3")])
    print("=" * 90)
    print(f"{folder.name}   ({len(files)} files)")
    print("-" * 90)
    vals = {f: Counter() for f in FIELDS}
    disc_track = Counter()
    rows = []
    for p in files:
        af = MFile(str(p), easy=True)
        g = lambda k: (af.get(k, [""])[0] if af else "")
        for f in FIELDS:
            vals[f][g(f)] += 1
        dt = (g("discnumber"), g("tracknumber"))
        disc_track[dt] += 1
        rows.append((g("discnumber"), g("tracknumber"), g("title"), g("album"),
                     g("albumartist"), g("date") or g("year")))
    for f in FIELDS:
        distinct = vals[f]
        if len(distinct) > 1:
            shown = ", ".join(f"{k!r}×{n}" for k, n in distinct.most_common())
            print(f"  ⚠ {f}: {len(distinct)} distinct -> {shown}")
        else:
            (only,) = distinct
            print(f"    {f}: {only!r}")
    dups = [k for k, n in disc_track.items() if n > 1]
    if dups:
        print(f"  ⚠ DUPLICATE (disc,track) keys: {dups}")
    missing_disc = sum(1 for r in rows if not r[0])
    if missing_disc:
        print(f"  ⚠ {missing_disc}/{len(files)} files have NO discnumber")
