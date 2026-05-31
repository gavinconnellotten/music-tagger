import json
from pathlib import Path

path = Path('sample_acoustid_green_day.json')
if not path.exists():
    print('MISSING')
    raise SystemExit(1)
with path.open('r', encoding='utf-8') as f:
    data = json.load(f)
print(json.dumps(data['summary'], indent=2, ensure_ascii=False))
