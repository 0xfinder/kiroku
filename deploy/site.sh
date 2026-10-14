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
commit=$(git -C "$repo" rev-parse HEAD)
for page in index.html 404.html; do
    if ! git -C "$repo" cat-file -e "$commit:site/$page" 2>/dev/null; then
        printf 'Refusing to publish: site/%s is missing from HEAD.\n' "$page" >&2
        exit 1
    fi
done
root=$(realpath -m -- "${ORULON_ROOT:-/srv/orulon}")
sha=$(git -C "$repo" rev-parse --short "$commit")
release="$root/releases/$(date -u +%Y%m%dT%H%M%SZ)-$sha"

if "$dry_run"; then
    printf 'Would publish %s HEAD:site (excluding tools/) to %s\n' "$repo" "$release"
    printf 'Would set new directories to 755 and release files to 644.\n'
    printf 'Would atomically replace %s/current with a symlink to %s\n' "$root" "$release"
    printf 'Would keep all old releases.\n'
    exit 0
fi

mkdir -p -m 755 -- "$root" "$root/releases"
# Exclusive creation refuses a same-second collision instead of modifying a served release.
mkdir -- "$release"
temporary=''
published=false
cleanup() {
    if [[ -n "$temporary" ]]; then rm -rf -- "$temporary"; fi
    if ! "$published"; then rm -rf -- "$release"; fi
}
trap cleanup EXIT
git -C "$repo" archive --format=tar "$commit:site" | tar -C "$release" -xf -
rm -rf -- "$release/tools"
find "$release" -type d -exec chmod 755 {} +
find "$release" -type f -exec chmod 644 {} +

temporary=$(mktemp -d "$root/.publish.XXXXXX")
ln -s -- "$release" "$temporary/current"
mv -Tf -- "$temporary/current" "$root/current"
published=true
printf '%s\n' "$release"
