#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
cargo build --release
mkdir -p dist/source/bin
cp addon/__init__.py dist/source/__init__.py
cp addon/streaming.py dist/source/streaming.py
cp addon/blender_manifest.toml dist/source/blender_manifest.toml
cp LICENSE dist/source/LICENSE
cp target/release/scan-cache dist/source/bin/scan-cache
chmod +x dist/source/bin/scan-cache
/Applications/Blender.app/Contents/MacOS/Blender --command extension build \
    --source-dir dist/source \
    --output-filepath dist/scan_viewer-0.4.0-macos-arm64.zip
/Applications/Blender.app/Contents/MacOS/Blender --command extension validate \
    dist/scan_viewer-0.4.0-macos-arm64.zip
