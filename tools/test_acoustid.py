import acoustid
import os
from pathlib import Path

# Load key from .venv/keys.txt
key = None
keys_path = Path('.venv/keys.txt')
if keys_path.exists():
    for line in keys_path.read_text(encoding='utf-8').splitlines():
        if '=' in line:
            k,v = line.split('=',1)
            if k.strip().lower().startswith('acoustid') or k.strip().lower().startswith('acoustidkey') or 'acoustid' in k.lower():
                key = v.strip()
                break

print('Using key:', key)

file_to_test = r"z:\Music\The Divine Comedy - Charmed Life - The Best Of The Divine Comedy (Deluxe Edition) (2022) Mp3 320kbps [PMEDIA] ★\10. Becoming More Like Alfie (2020 remaster).mp3"
print('Testing file:', file_to_test)

try:
    results = acoustid.match(key, file_to_test, meta='recordings releases releasegroups', parse=True)
    print('Results:')
    for r in results:
        print(r)
except Exception as e:
    import traceback
    traceback.print_exc()
    print('Exception repr:', repr(e))
