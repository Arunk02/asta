#!/bin/sh
set -eu

source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
app_dir=${1:-"$HOME/Applications/Asta Pet.app"}
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT

mkdir -p "$build_dir/Asta Pet.app/Contents/MacOS"
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos13.0" \
  -module-cache-path "$build_dir/module-cache" \
  "$source_dir/PetActivity.swift" "$source_dir/AstaPet.swift" \
  -o "$build_dir/Asta Pet.app/Contents/MacOS/AstaPet"

cat > "$build_dir/Asta Pet.app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.asta.pet</string>
  <key>CFBundleName</key><string>Asta Pet</string>
  <key>CFBundleExecutable</key><string>AstaPet</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

codesign --force --sign - "$build_dir/Asta Pet.app"
mkdir -p "$(dirname -- "$app_dir")"
ditto "$build_dir/Asta Pet.app" "$app_dir"
printf 'Built %s\n' "$app_dir"
