#!/bin/sh
# Repair system files found corrupted on this machine (magic != ELF, wrong md5).
# Run with sudo. Re-downloads the packages and rewrites their files.
set -e
PKGS="libopenjp2-7 libsoup-2.4-1 libjavascriptcoregtk-6.0-1 libpq5 netpbm ffmpeg libkate1"
if [ "$(id -u)" -ne 0 ]; then
    echo "run with: sudo $0"
    exit 1
fi
apt-get update
apt-get install --reinstall -y $PKGS
echo "--- verify:"
for f in /usr/lib/x86_64-linux-gnu/libopenjp2.so.2.5.0 \
         /usr/lib/x86_64-linux-gnu/libsoup-2.4.so.1.11.2 \
         /usr/lib/x86_64-linux-gnu/libjavascriptcoregtk-6.0.so.1.7.14 \
         /usr/lib/x86_64-linux-gnu/libpq.so.5.16 \
         /usr/bin/ffmpeg; do
    if [ -e "$f" ] && [ "$(od -An -N4 -tx1 "$f" | tr -d ' \n')" = "7f454c46" ]; then
        echo "OK   $f"
    else
        echo "FAIL $f"
    fi
done
