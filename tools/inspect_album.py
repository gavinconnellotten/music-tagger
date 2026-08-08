"""Ad-hoc: inspect what the latest run proposed/changed for albums matching a substring.

Usage: python tools/inspect_album.py "Piano Man"
Prints per-file the report's current vs proposed (changed fields only), plus the
album-level decision. Pass --disk to also re-read the live on-disk tags now.
"""
import json
import sys
from pathlib import Path

needle = sys.argv[1].lower()
show_disk = "--disk" in sys.argv

data = json.load(open("music_tagger_report.json", encoding="utf-8"))
hits = [r for r in data if needle in r["folder"].lower()]
if not hits:
    print("No album folder matches:", needle)
    sys.exit(0)

for r in hits:
    print("=" * 90)
    print(r["folder"])
    print(f"  recommendation={r['recommendation']}  action={r['action']}  "
          f"confidence={r['confidence']}  from_cache={r['from_cache']}")
    ch = r.get("chosen") or {}
    if ch:
        print(f"  matched -> {ch.get('albumartist')} / {ch.get('album')} "
              f"({ch.get('year')})  mbid={ch.get('album_id')}  distance={ch.get('distance')}")
    print(f"  files={r['n_files']}  files_with_changes={r['n_changed_files']}")
    print("-" * 90)
    for f in r["files"]:
        changed = f["changed"]
        flag = "CHANGED" if changed else "       "
        line = f"  [{flag}] {f['name']}"
        print(line)
        if changed:
            cur, prop = f["current"], f["proposed"]
            for k in changed:
                print(f"            {k}: {cur.get(k)!r}  ->  {prop.get(k)!r}")
        if show_disk:
            from mutagen import File as MFile
            try:
                af = MFile(f["path"], easy=True)
                tn = af.get("tracknumber", ["?"])[0] if af else "?"
                dn = af.get("discnumber", ["?"])[0] if af else "?"
                ti = af.get("title", ["?"])[0] if af else "?"
                ar = af.get("artist", ["?"])[0] if af else "?"
                print(f"            DISK: disc={dn} track={tn} title={ti!r} artist={ar!r}")
            except Exception as e:  # noqa: BLE001
                print(f"            DISK: <read error: {e!r}>")
