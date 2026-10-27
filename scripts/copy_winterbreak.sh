#!/usr/bin/env bash
# Copy the WinterBreak payload onto a connected Kindle.
#
# Run this only AFTER the Kindle has been put in Airplane mode and rebooted:
# the Kindle rewrites its store cache on shutdown and would clobber the payload.
#
# Usage: ./copy_winterbreak.sh [path/to/WinterBreak.tar.gz]
set -euo pipefail

MOUNT="${KINDLE_MOUNT:-/Volumes/Kindle}"
TARBALL="${1:-}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [ ! -f "$MOUNT/system/version.txt" ]; then
  echo "error: no Kindle at $MOUNT (connect by USB and wait for it to mount)" >&2
  exit 1
fi
echo "Kindle:   $MOUNT"
echo "Firmware: $(tr -d '\r' < "$MOUNT/system/version.txt")"

case "$(tr -d '\r' < "$MOUNT/system/version.txt")" in
  *5.18.[1-9]*|*5.19.*|*5.2[0-9].*)
    echo "error: this firmware is at or past 5.18.1 — WinterBreak will not work." >&2
    exit 1 ;;
esac

if [ -z "$TARBALL" ]; then
  TARBALL="$STAGE/WinterBreak.tar.gz"
  echo "Downloading WinterBreak..."
  curl -fsSL -o "$TARBALL" \
    https://github.com/KindleModding/WinterBreak/releases/latest/download/WinterBreak.tar.gz
fi
echo "sha256:   $(shasum -a 256 "$TARBALL" | cut -d' ' -f1)"

# Extract to a staging dir, never straight onto the Kindle.
mkdir -p "$STAGE/payload"
tar xzf "$TARBALL" -C "$STAGE/payload"

for item in .active_content_sandbox mesquito apps; do
  if [ ! -e "$STAGE/payload/$item" ]; then
    echo "error: payload missing $item" >&2
    exit 1
  fi
done

echo "Removing stale update files..."
find "$MOUNT" -maxdepth 1 \( -name 'update*.bin' -o -name 'update.bin.tmp.partial' \) \
  -print -delete 2>/dev/null || true

echo "Copying payload..."
# Merge into existing directories. `cp -R src dst/` would NEST src inside an
# already-present dst/src (the Kindle ships with .active_content_sandbox), so
# copy the contents with a trailing /. and create the target first.
for item in .active_content_sandbox mesquito apps; do
  mkdir -p "$MOUNT/$item"
  # COPYFILE_DISABLE stops macOS writing ._* AppleDouble sidecars onto FAT32.
  COPYFILE_DISABLE=1 cp -R "$STAGE/payload/$item/." "$MOUNT/$item/"
  echo "  merged $item"
done
find "$MOUNT" -maxdepth 2 -name '._*' -delete 2>/dev/null || true

sync
echo
echo "Verifying on device:"
for item in .active_content_sandbox mesquito apps; do
  [ -e "$MOUNT/$item" ] && echo "  ok  $item" || { echo "  MISSING  $item" >&2; exit 1; }
done
[ -f "$MOUNT/apps/tech.hackerdude.winterbreak/index.html" ] \
  && echo "  ok  WinterBreak app present" \
  || { echo "  MISSING  WinterBreak app" >&2; exit 1; }
[ -f "$MOUNT/mesquito/index.html" ] \
  && echo "  ok  Mesquito present" || { echo "  MISSING  Mesquito" >&2; exit 1; }
[ -f "$MOUNT/.active_content_sandbox/store/resource/cachedResources/index.html" ] \
  && echo "  ok  store cache replaced" \
  || { echo "  MISSING  store cache" >&2; exit 1; }
# Guard against the cp -R nesting mistake.
for item in .active_content_sandbox mesquito apps; do
  if [ -e "$MOUNT/$item/$item" ]; then
    echo "  ERROR  nested $item/$item — payload copied wrongly" >&2
    exit 1
  fi
done

echo
echo "Done. Now, on the Kindle:"
echo "  1. Eject it from the Mac."
echo "  2. Tap the Kindle Store (cart) icon on the home screen."
echo "  3. Tap Yes to turn off Airplane mode."
echo "  4. When Mesquito loads, tap the WinterBreak icon."
echo "  5. Wait ~30s for text to appear and the UI to restart."
