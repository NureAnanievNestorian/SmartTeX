#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GO_DIR="$ROOT_DIR/local_agent/go"
CHANNEL="${SMARTTEX_LOCAL_UPDATE_CHANNEL:-stable}"
VERSION="${SMARTTEX_LOCAL_VERSION:-$(cd "$GO_DIR" && go run ./cmd/smarttex-local-go version 2>/dev/null || true)}"

if [[ -z "${VERSION}" ]]; then
  VERSION="$(grep -E 'const toolVersion =' "$GO_DIR/cmd/smarttex-local-go/main.go" | sed -E 's/.*"([^"]+)".*/\1/')"
fi

OUT_DIR="$ROOT_DIR/projects/static/local-agent/$CHANNEL"
mkdir -p "$OUT_DIR"

platforms=(
  "darwin arm64"
  "darwin amd64"
  "linux arm64"
  "linux amd64"
)

assets_json=""
for platform in "${platforms[@]}"; do
  read -r goos goarch <<<"$platform"
  name="smarttex-local-go-${goos}-${goarch}"
  if [[ "$goos" == "windows" ]]; then
    name="${name}.exe"
  fi
  echo "Building $name"
  (
    cd "$GO_DIR"
    GOOS="$goos" GOARCH="$goarch" CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$OUT_DIR/$name" ./cmd/smarttex-local-go
  )
  sha="$(shasum -a 256 "$OUT_DIR/$name" | awk '{print $1}')"
  asset="$(python3 - <<PY
import json
print(json.dumps({
    "os": "$goos",
    "arch": "$goarch",
    "url": "/static/local-agent/$CHANNEL/$name",
    "sha256": "$sha",
}))
PY
)"
  if [[ -n "$assets_json" ]]; then
    assets_json="$assets_json,$asset"
  else
    assets_json="$asset"
  fi
done

python3 - <<PY > "$OUT_DIR/manifest.json"
import json
from pathlib import Path

toolchains = []
toolchain_manifest = "${SMARTTEX_TOOLCHAIN_MANIFEST:-}"
if toolchain_manifest:
    path = Path(toolchain_manifest)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            toolchains = payload.get("toolchains") or []
        elif isinstance(payload, list):
            toolchains = payload

print(json.dumps({
    "version": "$VERSION",
    "channel": "$CHANNEL",
    "assets": [$assets_json],
    "toolchains": toolchains,
}, indent=2, ensure_ascii=False))
PY

echo "Wrote $OUT_DIR/manifest.json"
