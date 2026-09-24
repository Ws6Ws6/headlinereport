# HEADLINE REPORT

Drudge-style static news aggregator. Fetches real RSS/Atom headlines at build time and writes `public/index.html`.

Live site: https://headlinereport.net (GitHub Pages)

## Quick start

```bash
cd /workspace/drudge-site
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python build.py
# or: ./run.sh
```

Site name and feed list live in `config.py`.

## Publish (gh-pages fallback)

Because the GitHub token used for setup lacks the `workflow` scope, hourly updates run from this machine via:

```bash
./publish.sh
```

That rebuilds `public/index.html` and force-pushes an orphan `gh-pages` branch. A GitHub Actions workflow is kept locally at `.github/workflows/pages.yml` for later use once a token with `workflow` scope is available.

Serve locally:

```bash
python3 -m http.server 8877 --bind 127.0.0.1 --directory public
```
