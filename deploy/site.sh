#!/usr/bin/env bash
set -euo pipefail
umask 022

dry_run=false
if [[ $# -eq 1 && $1 == --dry-run ]]; then
    dry_run=true
elif [[ $# -ne 0 ]]; then
    printf 'Usage: %s [--dry-run]\n' "$0" >&2
    exit 2
fi

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for page in index.html 404.html; do
    if [[ ! -f "$repo/site/$page" ]]; then
        printf 'Refusing to publish: site/%s is missing.\n' "$page" >&2
        exit 1
    fi
done
if [[ -n $(git -C "$repo" status --porcelain --untracked-files=all --ignored -- site/) ]]; then
    printf 'Refusing to publish: git worktree is dirty under site/. Commit or remove those changes first.\n' >&2
    exit 1
fi

root=$(realpath -m -- "${ORULON_ROOT:-/srv/orulon}")
sha=$(git -C "$repo" rev-parse --short HEAD)
release="$root/releases/$(date -u +%Y%m%dT%H%M%SZ)-$sha"

if "$dry_run"; then
    printf 'Would copy %s/site/ (excluding tools/) to %s\n' "$repo" "$release"
    printf 'Would set directories to 755 and files to 644.\n'
    printf 'Would atomically replace %s/current with a symlink to %s\n' "$root" "$release"
    printf 'Would keep all old releases.\n'
    exit 0
fi

mkdir -p -- "$root/releases"
chmod 755 -- "$root" "$root/releases"
# Exclusive creation refuses a same-second collision instead of modifying a served release.
mkdir -- "$release"
tar -C "$repo/site" --exclude='./tools' -cf - . | tar -C "$release" -xf -
find "$release" -type d -exec chmod 755 {} +
find "$release" -type f -exec chmod 644 {} +

temporary=$(mktemp -d "$root/.publish.XXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
ln -s -- "$release" "$temporary/current"
mv -Tf -- "$temporary/current" "$root/current"
printf '%s\n' "$release"
