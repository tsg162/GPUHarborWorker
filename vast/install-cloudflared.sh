#!/usr/bin/env bash
set -euo pipefail
case "$(uname -m)" in
  x86_64|amd64) arch=amd64; expected=77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2 ;;
  aarch64|arm64) arch=arm64; expected=aaeb2d7d0da3614634c7e03ab13487a1522c2e79165ed2929cfe23d5e95b326d ;;
  *) echo 'Unsupported cloudflared architecture' >&2; exit 1 ;;
esac
temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT
curl -fsSL --retry 3 "https://github.com/cloudflare/cloudflared/releases/download/2026.9.3/cloudflared-linux-${arch}" -o "$temporary"
printf '%s  %s\n' "$expected" "$temporary" | sha256sum -c -
install -m 755 "$temporary" /usr/local/bin/cloudflared
