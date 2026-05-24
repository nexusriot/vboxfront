#!/usr/bin/env bash
# vboxfront — Debian package assembly
#
# Wraps the PyInstaller binary produced by scripts/build.sh into a .deb for the
# host architecture. Uses plain dpkg-deb; no debhelper needed.
#
# Output: dist/vboxfront_<version>_<arch>.deb

set -euo pipefail

cd "$(dirname "$0")/.."

ROOT="$PWD"
APP="vboxfront"
VERSION="${VERSION:-1.0.0}"
VERSION="${VERSION#v}"                       # drop leading "v"
# Debian requires Version to start with a digit. `git describe --always`
# falls back to a bare commit hash when no tags exist — prefix it so dpkg
# accepts it (e.g. "dee2e72-dirty" -> "0.0.0~git.dee2e72-dirty").
case "$VERSION" in
    [0-9]*) : ;;
    *)      VERSION="0.0.0~git.${VERSION}" ;;
esac
ARCH="${ARCH:-$(dpkg --print-architecture 2>/dev/null || uname -m)}"

BIN="$ROOT/dist/$APP"
if [ ! -f "$BIN" ]; then
    echo ">> binary $BIN not found; building first"
    "$ROOT/scripts/build.sh"
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

install -d "$STAGE/DEBIAN"
install -d "$STAGE/usr/bin"
install -d "$STAGE/usr/share/applications"
install -d "$STAGE/usr/share/doc/$APP"

install -m 0755 "$BIN"                              "$STAGE/usr/bin/$APP"
install -m 0644 "$ROOT/scripts/deb/$APP.desktop"      "$STAGE/usr/share/applications/$APP.desktop"

# changelog (gzipped, as required by lintian)
if [ -f "$ROOT/scripts/deb/changelog" ]; then
    gzip -9n -c "$ROOT/scripts/deb/changelog" \
        > "$STAGE/usr/share/doc/$APP/changelog.Debian.gz"
fi

if [ -f "$ROOT/README.md" ]; then
    install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/$APP/README.md"
fi

INSTALLED_SIZE=$(du -sk "$STAGE/usr" | cut -f1)
sed -e "s/@VERSION@/${VERSION}/g" \
    -e "s/@ARCH@/${ARCH}/g" \
    -e "s/@SIZE@/${INSTALLED_SIZE}/g" \
    "$ROOT/scripts/deb/DEBIAN/control.tpl" > "$STAGE/DEBIAN/control"

( cd "$STAGE" && find usr -type f -print0 \
    | xargs -0 md5sum > "$STAGE/DEBIAN/md5sums" )
chmod 0644 "$STAGE/DEBIAN/md5sums"

mkdir -p "$ROOT/dist"
DEB="$ROOT/dist/${APP}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB"

echo
echo "Built: $DEB"
dpkg-deb -I "$DEB" || true
