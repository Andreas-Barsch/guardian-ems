#!/usr/bin/env sh
set -eu

VERSION="$(grep '^version:' guardian_battery/config.yaml | sed 's/version:[[:space:]]*//;s/"//g')"
echo "Guardian Battery Version: ${VERSION}"
git status --short
echo
echo "Vor dem Push prüfen:"
echo "  git add ."
echo "  git commit -m \"Guardian Battery ${VERSION}\""
echo "  git tag \"v${VERSION}\""
echo "  git push origin main --tags"
