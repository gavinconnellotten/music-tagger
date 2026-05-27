# Music Tagger

Automated music metadata tagging using AcoustID, MusicBrainz, and Claude AI.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Usage

```powershell
python music_tagger.py C:\path\to\music --dry-run
python music_tagger.py C:\path\to\music --confidence 80
```

Or install the package and run the CLI:

```powershell
python -m pip install -e .
music-tagger C:\path\to\music
```

## Requirements

- Python 3.11+
- `fpcalc` from Chromaprint installed and on PATH
- `ACOUSTID_API_KEY` environment variable
- `ANTHROPIC_API_KEY` environment variable

## Recommended

Set environment variables in PowerShell:

```powershell
$env:ACOUSTID_API_KEY = "your_key_here"
$env:ANTHROPIC_API_KEY = "your_key_here"
```
