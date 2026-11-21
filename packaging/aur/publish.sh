#!/bin/bash
# Update an AUR package from packaging/aur/<name>.
#
#   packaging/aur/publish.sh [luxtide-git]          prepare build/aur/<name>
#                                                   and show what would change
#   packaging/aur/publish.sh [luxtide-git] --push   then commit and push it
#
# The AUR repository is cloned over SSH (aur@aur.archlinux.org), so the key
# registered on the AUR account must be loaded or named in ~/.ssh/config.
# Commits there are public and carry this repository's user.name and
# user.email; check them first.
#
# A -git package builds the newest commit on GitHub by itself, so it needs a
# new push only when the PKGBUILD changes. The pkgver written into it is the
# one of this checkout's HEAD, which should therefore be pushed to GitHub
# first.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
name=luxtide-git
push=0
for arg in "$@"; do
    case "$arg" in
        --push) push=1 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) name=$arg ;;
    esac
done
src=$here/$name
[[ -f $src/PKGBUILD ]] || { echo "no $src/PKGBUILD" >&2; exit 1; }
work=$root/build/aur/$name

author=$(git -C "$root" config user.name)
email=$(git -C "$root" config user.email)
echo "AUR commits will be signed off as: $author <$email>"

if [[ -d $work/.git ]]; then
    # A package not pushed yet has no master to pull.
    if git -C "$work" ls-remote --exit-code --heads origin master >/dev/null; then
        git -C "$work" pull --ff-only --quiet origin master
    fi
else
    mkdir -p "$(dirname "$work")"
    git clone "ssh://aur@aur.archlinux.org/$name.git" "$work"
fi

cp "$src/PKGBUILD" "$work/PKGBUILD"
cp "$root/packaging/arch/luxtide.install" "$work/luxtide.install"

if [[ $name == *-git ]]; then
    release=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$root/luxtide/__init__.py")
    pkgver=$release.r$(git -C "$root" rev-list --count HEAD).g$(git -C "$root" rev-parse --short=7 HEAD)
    sed -i "s/^pkgver=.*/pkgver=$pkgver/" "$work/PKGBUILD"
    if [[ -n $(git -C "$root" status --porcelain --untracked-files=no) ]]; then
        echo "note: uncommitted changes here are not part of $pkgver" >&2
    fi
    if ! git -C "$root" branch -r --contains HEAD 2>/dev/null | grep -q 'origin/main'; then
        echo "note: HEAD is not on origin/main yet; push it to GitHub first" >&2
    fi
fi

(cd "$work" && makepkg --printsrcinfo > .SRCINFO)
git -C "$work" add PKGBUILD luxtide.install .SRCINFO
if git -C "$work" diff --cached --quiet; then
    echo "Nothing to change in $name."
    exit 0
fi
git -C "$work" --no-pager diff --cached --stat
git -C "$work" --no-pager diff --cached -- PKGBUILD

if (( ! push )); then
    echo
    echo "Prepared in $work; run again with --push to publish."
    exit 0
fi

version=$(sed -n 's/^\tpkgver = //p' "$work/.SRCINFO")-$(sed -n 's/^\tpkgrel = //p' "$work/.SRCINFO")
if git -C "$work" rev-parse --verify --quiet HEAD >/dev/null; then
    message="Update to $version"
else
    message="Initial import: $version"
fi
git -C "$work" -c user.name="$author" -c user.email="$email" commit --quiet -m "$message"
git -C "$work" push origin HEAD:master
echo "Published $name $version: https://aur.archlinux.org/packages/$name"
