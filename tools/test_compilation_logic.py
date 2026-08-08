"""Offline sanity test for the compilation normalization logic (no files/network)."""
from music_tagger.tags import diff_tags, COMPILATION
from music_tagger.__main__ import _album_is_va, _compilation_fix

# VA detection
assert _album_is_va([{"albumartist": "Various Artists", "artist": "A"},
                     {"albumartist": "Various Artists", "artist": "B"}]) is True
assert _album_is_va([{"albumartist": "Billy Joel", "artist": "Billy Joel"}]*3) is False
assert _album_is_va([{"artist": "A"}, {"artist": "B"}]) is True            # no albumartist, mixed
assert _album_is_va([{"albumartist": "Eagles", "artist": "Eagles"}]) is False

# compilation fix decision
assert _compilation_fix({"compilation": "PMEDIA"}, False) == {COMPILATION: ""}    # single -> clear
assert _compilation_fix({"compilation": "PMEDIA"}, True) == {COMPILATION: "1"}    # VA -> set 1
assert _compilation_fix({"compilation": "1"}, True) == {}                          # legit, leave
assert _compilation_fix({"compilation": "0"}, False) == {}
assert _compilation_fix({}, False) == {}                                           # absent, leave

# diff detects clear + set, ignores no-op
assert diff_tags({"compilation": "PMEDIA"}, {COMPILATION: ""}) == [COMPILATION]
assert diff_tags({"compilation": "PMEDIA"}, {COMPILATION: "1"}) == [COMPILATION]
assert diff_tags({}, {COMPILATION: ""}) == []                                      # nothing to clear
assert diff_tags({"compilation": "1"}, {COMPILATION: "1"}) == []                   # no-op

print("ALL COMPILATION LOGIC TESTS PASSED")
