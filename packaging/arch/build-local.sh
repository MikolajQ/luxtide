#!/bin/bash
# Build the Arch package from this checkout instead of a GitHub release.
#
# The PKGBUILD downloads a tagged release. This script makes the same tarball
# from the checked-out commit with git archive and puts it where makepkg looks
# before downloading, so nothing is fetched. Off a release tag the version gets
# a .rN.gHASH suffix, so every newer commit installs as an upgrade.
#
# --dirty packages the working tree as it is, uncommitted and new files
# included (what .gitignore leaves out stays out), for trying changes on
# another machine before committing them. The version gets a .dYYYYMMDDHHMM
# suffix: each such build upgrades the previous one, and the first commit
# after it upgrades it again.
#
#   packaging/arch/build-local.sh [--dirty] [makepkg options, e.g. --nocheck]
#   sudo pacman -U build/arch/luxtide-*.pkg.tar.zst
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
out=$root/build/arch

dirty=0
if [[ ${1:-} == --dirty ]]; then
    dirty=1
    shift
fi

if (( ! dirty )) && [[ -n $(git -C "$root" status --porcelain) ]]; then
    echo "Uncommitted or new files would be missing from the package; commit them" >&2
    echo "first, or build the working tree as it is with --dirty." >&2
    exit 1
fi

pkgname=$(sed -n 's/^pkgname=//p' "$here/PKGBUILD")
release=$(sed -n 's/^pkgver=//p' "$here/PKGBUILD")
code=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$root/luxtide/__init__.py")
if [[ $release != "$code" ]]; then
    echo "PKGBUILD says $release but luxtide/__init__.py says $code." >&2
    exit 1
fi

if git -C "$root" describe --exact-match --tags --match "v$release" HEAD >/dev/null 2>&1; then
    pkgver=$release
else
    pkgver=$release.r$(git -C "$root" rev-list --count HEAD).g$(git -C "$root" rev-parse --short HEAD)
fi

tree=HEAD
if (( dirty )); then
    # The working tree as a git tree, through a throwaway index: the real
    # index and history are left as they are.
    index=$(mktemp)
    trap 'rm -f "$index"' EXIT
    cp "$(git -C "$root" rev-parse --absolute-git-dir)/index" "$index"
    GIT_INDEX_FILE=$index git -C "$root" add --all
    tree=$(GIT_INDEX_FILE=$index git -C "$root" write-tree)
    pkgver=$pkgver.d$(date +%Y%m%d%H%M)
fi

rm -rf "$out"
mkdir -p "$out"
tarball=$out/$pkgname-$pkgver.tar.gz
git -C "$root" archive --format=tar.gz --prefix="$pkgname-$pkgver/" -o "$tarball" "$tree"
cp "$here/PKGBUILD" "$here/$pkgname.install" "$out/"
sum=$(sha256sum "$tarball" | cut -d' ' -f1)
sed -i -e "s/^pkgver=.*/pkgver=$pkgver/" \
       -e "s/^sha256sums=.*/sha256sums=('$sum')/" "$out/PKGBUILD"

cd "$out"
# Keep everything here, whatever makepkg.conf says, so the result is easy to find.
PKGDEST=$out SRCDEST=$out BUILDDIR=$out makepkg --force --cleanbuild "$@"
echo "Package: $(ls "$out"/*.pkg.tar.*)"
