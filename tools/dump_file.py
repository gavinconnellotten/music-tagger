"""Dump every tag frame of one audio file (ground truth). Usage: dump_file.py <path>"""
import sys
from mutagen import File as MFile

path = sys.argv[1]
print(f"FILE: {path}\n" + "-" * 70)
raw = MFile(path)
if raw is None or raw.tags is None:
    print("(no tags)")
else:
    for k in sorted(raw.tags.keys()):
        v = raw.tags[k]
        v = v.text if hasattr(v, "text") else v
        print(f"  {k!r}: {v!r}")
print("-" * 70)
easy = MFile(path, easy=True)
print(f"  easy artist      : {easy.get('artist') if easy else None!r}")
print(f"  easy albumartist : {easy.get('albumartist') if easy else None!r}")
