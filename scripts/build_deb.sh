#!/usr/bin/env bash
# Builds myvoice_<version>_amd64.deb from the current source tree.
#
# The .deb ships only the app's own source (small); faster-whisper, numpy
# and friends are installed by DEBIAN/postinst at package-install time,
# using internet access. Whisper model weights are never bundled and are
# downloaded by the app itself on first use.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEBIAN_SRC="$HERE/debian"
BUILD_ROOT="$HERE/build/deb"
OUT_DIR="$HERE/dist"
ARCH="amd64"
PKG_NAME="myvoice"

VERSION=$(python3 -c "
import tomllib
with open('$HERE/pyproject.toml', 'rb') as f:
    print(tomllib.load(f)['project']['version'])
")

echo "==> Building ${PKG_NAME} ${VERSION} (${ARCH})"

PKG_ROOT="$BUILD_ROOT/${PKG_NAME}_${VERSION}_${ARCH}"
rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/DEBIAN"
mkdir -p "$PKG_ROOT/usr/bin"
mkdir -p "$PKG_ROOT/usr/lib/myvoice/src"
mkdir -p "$PKG_ROOT/usr/share/applications"

# ---- App source (small: just this project's own code) --------------------
cp -r "$HERE/myvoice" "$PKG_ROOT/usr/lib/myvoice/src/myvoice"
cp "$HERE/requirements.txt" "$PKG_ROOT/usr/lib/myvoice/src/"
cp "$HERE/pyproject.toml" "$PKG_ROOT/usr/lib/myvoice/src/"
find "$PKG_ROOT/usr/lib/myvoice/src" -name '__pycache__' -type d -exec rm -rf {} +

# ---- Launcher + desktop entry ---------------------------------------------
install -m 0755 "$DEBIAN_SRC/myvoice.wrapper" "$PKG_ROOT/usr/bin/myvoice"
install -m 0644 "$DEBIAN_SRC/myvoice.desktop" "$PKG_ROOT/usr/share/applications/myvoice.desktop"

# ---- Control files ----------------------------------------------------
sed "s/@VERSION@/$VERSION/" "$DEBIAN_SRC/control.in" > "$PKG_ROOT/DEBIAN/control"
install -m 0755 "$DEBIAN_SRC/postinst" "$PKG_ROOT/DEBIAN/postinst"
install -m 0755 "$DEBIAN_SRC/postrm" "$PKG_ROOT/DEBIAN/postrm"

# ---- Build ------------------------------------------------------------
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$PKG_ROOT" "$OUT_FILE"

echo "==> Built: $OUT_FILE"
echo "==> Install on the target machine with:"
echo "      sudo apt install ./$(basename "$OUT_FILE")"
