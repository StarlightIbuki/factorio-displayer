#!/usr/bin/env bash
# Publish the web app (src/factorio_display/api/static) to GitHub Pages.
#
# Creates an orphan `gh-pages` branch containing exactly the static files
# (plus a .nojekyll marker) and force-pushes it, then enables Pages for the
# repo if it isn't already.
#
# Usage:
#   bash deploy/deploy_ghpages.sh          # pushes the site
#   bash deploy/deploy_ghpages.sh --only-push   # skip the Pages-enable step
#
set -euo pipefail

REPO_URL="https://github.com/StarlightIbuki/factorio-displayer.git"
OWNER_REPO="${REPO_URL#https://github.com/}"
OWNER_REPO="${OWNER_REPO%.git}"
BRANCH="gh-pages"
SRC_STATIC="$(cd "$(dirname "$0")/.." && pwd)/src/factorio_display/api/static"

if [ ! -d "$SRC_STATIC" ]; then
  echo "static dir not found: $SRC_STATIC" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> preparing $BRANCH from $SRC_STATIC"
git clone --quiet --branch main "$REPO_URL" "$TMP/site"
cd "$TMP/site"
git checkout --quiet --orphan "$BRANCH"
git rm -rfq --ignore-unmatch . >/dev/null 2>&1 || true
# Remove leftover untracked files — but never delete .git (the orphan branch
# needs it).  `rm -rf ./.??*` would glob to .git and break the repo.
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} + 2>/dev/null || true

cp -r "$SRC_STATIC"/. .
touch .nojekyll

git add -A
if git diff --cached --quiet; then
  echo "no changes — gh-pages already up to date"
else
  git -c user.name="deploy" -c user.email="deploy@users.noreply.github.com" \
      commit -qm "deploy web app to GitHub Pages"
  echo "==> force-pushing $BRANCH"
  git push --quiet --force origin "$BRANCH"
  echo "pushed $BRANCH"
fi

if [ "${1:-}" != "--only-push" ] && command -v gh >/dev/null 2>&1; then
  echo "==> ensuring Pages is enabled (branch: $BRANCH)"
  gh api "repos/$OWNER_REPO/pages" >/dev/null 2>&1 \
    || gh api --method POST "repos/$OWNER_REPO/pages" \
        -f "source[branch]=$BRANCH" -f "source[path]=/" \
      >/dev/null
  gh api "repos/$OWNER_REPO/pages" --jq '.html_url' 2>/dev/null \
    && echo "   (Pages URL above)" \
    || echo "   (Pages may take a minute to build; check https://github.com/$OWNER_REPO/settings/pages)"
fi

echo "done"
