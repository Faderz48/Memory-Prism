#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  build-essential ca-certificates curl file libfuse2 libusb-1.0-0 \
  python3 python3-cryptography python3-pip python3-pyqt5

python3 -m pip install --quiet \
  "numpy==1.24.4" \
  "moderngl==5.8.2" \
  "pyinstaller==6.14.2"

root=/src
build_root=/tmp/memory-prism-build
version=$(sed -n 's/^APP_VERSION = "\([^"]*\)"/\1/p' "$root/version.py")
output="$root/dist/Memory-Prism-v${version}-x86_64.AppImage"
mkdir -p "$build_root" "$root/dist"

make -C "$root/third_party/ps2vmc-tool" clean >/dev/null
make -C "$root/third_party/ps2vmc-tool" -j"$(nproc)" src/main >/dev/null

python3 -m PyInstaller \
  --noconfirm --clean --windowed \
  --name memory-prism \
  --distpath "$build_root/dist" \
  --workpath "$build_root/pyinstaller" \
  --specpath "$build_root" \
  --collect-all moderngl \
  --collect-all glcontext \
  --add-binary "$root/third_party/ps2vmc-tool/ps2vmc-tool:bin" \
  "$root/app.py"

appdir="$build_root/MemoryPrism.AppDir"
mkdir -p "$appdir/usr/bin"
cp -a "$build_root/dist/memory-prism" "$appdir/usr/bin/memory-prism"

# Current Mesa drivers need the host C++ runtime and GBM libraries.
find "$appdir/usr/bin/memory-prism/_internal" -maxdepth 1 \
  \( -name 'libstdc++.so.6' -o -name 'libgcc_s.so.1' -o -name 'libgbm.so.1' \) \
  -delete

cp "$root/packaging/AppRun" "$appdir/AppRun"
cp "$root/packaging/memory-prism.desktop" "$appdir/memory-prism.desktop"
cp "$root/packaging/memory-prism.svg" "$appdir/memory-prism.svg"
ln -s memory-prism.svg "$appdir/.DirIcon"
chmod +x "$appdir/AppRun"

curl -L --fail --silent --show-error \
  -o "$build_root/appimagetool.AppImage" \
  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x "$build_root/appimagetool.AppImage"
ARCH=x86_64 "$build_root/appimagetool.AppImage" --appimage-extract-and-run \
  "$appdir" "$output"
chmod +x "$output"
(cd "$root/dist" && sha256sum "$(basename "$output")" > "$(basename "$output").sha256")
echo "Built $output"
