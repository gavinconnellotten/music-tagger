"""Offline test for pick_album: cleanliness-first canonical album-name selection."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.normalize_albums import pick_album, pick_majority

cases = [
    # (counter, folder, expected)
    (Counter({"Absent Friends": 11, "Absent Friends (2004, Reissue) CD 2": 18}),
     "(2020) The Divine Comedy - Absent Friends (2004, Reissue) [FLAC]", "Absent Friends"),
    (Counter({"Rated R": 13, "R": 3}), "[2000] Rated R [2 CD]", "Rated R"),
    (Counter({"OK Computer": 18, "OK Computer OKNOTOK 1997 2017": 2, "College Karma EP": 2}),
     "1997 - Ok Computer", "OK Computer"),
    (Counter({"Insomniac": 14, "Insomniac Ltd.Ed. (CD2)": 6}), "1995 - Insomniac Ltd.Ed", "Insomniac"),
    (Counter({"Queen II": 12, "Queen II (1994. Digital Remaster EMI)": 2}),
     "1974 Queen II (Digital Remaster + Bonus tracks)", "Queen II"),
    (Counter({"Pablo Honey": 28, "Creep": 2, "My Iron Lung": 1}), "1993 - Pablo Honey", "Pablo Honey"),
    # case-only: either is fine, but should be deterministic majority
    (Counter({"The Best of Bob Dylan": 9, "The Best Of Bob Dylan": 9}),
     "Bob Dylan - The Best Of Bob Dylan (Remastered 1997)", "The Best of Bob Dylan"),
    (Counter({"Labour of Love III": 8, "Labour Of Love III": 7}),
     "(1998) - UB40 - Labour Of Love III", "Labour of Love III"),
]
for counter, folder, expected in cases:
    got = pick_album(counter, folder)
    status = "ok " if got == expected else "FAIL"
    print(f"  [{status}] {folder[:40]:42} -> {got!r}")
    assert got == expected, f"expected {expected!r}, got {got!r}"

# albumartist still count-first
assert pick_majority(Counter({"Q-Tip": 8, "Q‐Tip": 4}), "Q-Tip - The Renaissance") == "Q-Tip"
print("ALL pick_album TESTS PASSED")
