#!/usr/bin/env bash
# Rebuild the site and force-push an orphan gh-pages branch for GitHub Pages.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
REPO_URL="$(git remote get-url origin)"

# Ensure deps + rebuild
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
.venv/bin/python build.py

# Stage deploy tree
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp public/index.html "$STAGE/index.html"
cp public/robots.txt "$STAGE/robots.txt"
cp public/sitemap.xml "$STAGE/sitemap.xml"
printf 'headlinereport.net\n' > "$STAGE/CNAME"
touch "$STAGE/.nojekyll"

# Fresh orphan commit + force-push gh-pages
git -C "$STAGE" init -b gh-pages
git -C "$STAGE" config user.email "ws6ws6@users.noreply.github.com"
git -C "$STAGE" config user.name "Ws6Ws6"
git -C "$STAGE" add index.html robots.txt sitemap.xml CNAME .nojekyll
git -C "$STAGE" commit -m "Publish HEADLINE REPORT $(date -R)"
git -C "$STAGE" remote add origin "$REPO_URL"
git -C "$STAGE" push -f origin gh-pages

echo "Published to gh-pages."
