# Library Checker Lite

A no-cost, low-maintenance mobile report for checking which Goodreads "to-read" books appear available at selected OverDrive libraries.

This project is designed for GitHub Pages + GitHub Actions:
- Actions run on a schedule (or manually)
- A Python script builds `docs/results.json`
- GitHub Pages serves a mobile-friendly `docs/index.html`

## How It Works

1. Set your Goodreads RSS URL as a GitHub secret.
2. Configure libraries in `libraries.json`.
3. GitHub Action runs `scripts/generate_report.py`.
4. `docs/results.json` is updated and committed.
5. Open GitHub Pages URL on mobile to see latest results.

## Setup

### 1. Create a new GitHub repo

Create a new repository (for example: `library-checker-lite`) and push this folder to it.

### 2. Add secret

In GitHub repo settings, add:
- `GOODREADS_RSS_URL` = your Goodreads RSS URL

### 3. Configure libraries

Edit `libraries.json` and set the libraries you want to check.

### 4. Enable GitHub Pages

In repo settings:
- Pages source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`

### 5. Run workflow

Go to Actions and run `Generate Library Report` once manually.

## Local Run (optional)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
GOODREADS_RSS_URL='https://www.goodreads.com/review/list_rss/...' python scripts/generate_report.py
```

Then open `docs/index.html` in a browser.

## Notes

- This checks availability heuristically from OverDrive search pages.
- Links are generated for Libby/OverDrive search handoff.
- No login credentials are stored or required.
