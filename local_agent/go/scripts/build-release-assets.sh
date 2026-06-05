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
  "windows arm64"
  "windows amd64"
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

cat > "$OUT_DIR/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

channel="${SMARTTEX_LOCAL_UPDATE_CHANNEL:-stable}"
bin_dir="${SMARTTEX_LOCAL_BIN_DIR:-$HOME/.local/bin}"
server="${SMARTTEX_SERVER:-}"

if [[ -z "$server" ]]; then
  if [[ "$0" == http://* || "$0" == https://* ]]; then
    server="$(printf "%s" "$0" | sed -E 's#^(https?://[^/]+).*#\1#')"
  else
    server="https://smart-tex.pp.ua"
  fi
fi

base_url="${server%/}/static/local-agent/${channel}"
manifest_url="$base_url/manifest.json"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "$os" in
  darwin|linux) ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac
case "$arch" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64) arch="amd64" ;;
  *) echo "Unsupported architecture: $arch" >&2; exit 1 ;;
esac

echo "Fetching SmartTeX Local Agent manifest: $manifest_url"
curl -fsSL "$manifest_url" -o "$tmp_dir/manifest.json"

asset_json="$(python3 - "$tmp_dir/manifest.json" "$os" "$arch" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
target_os, target_arch = sys.argv[2], sys.argv[3]
for asset in manifest.get("assets", []):
    if asset.get("os") == target_os and asset.get("arch") == target_arch:
        print(json.dumps({"version": manifest.get("version", ""), **asset}))
        break
else:
    raise SystemExit(f"No SmartTeX Local Agent binary for {target_os}/{target_arch}")
PY
)"

asset_url="$(python3 - <<PY
import json
payload = json.loads('''$asset_json''')
print(payload["url"])
PY
)"
asset_sha="$(python3 - <<PY
import json
payload = json.loads('''$asset_json''')
print(payload["sha256"])
PY
)"
version="$(python3 - <<PY
import json
payload = json.loads('''$asset_json''')
print(payload.get("version", ""))
PY
)"

if [[ "$asset_url" != http://* && "$asset_url" != https://* ]]; then
  asset_url="${server%/}${asset_url}"
fi

echo "Downloading SmartTeX Local Agent ${version:-latest}: $asset_url"
curl -fsSL "$asset_url" -o "$tmp_dir/smarttex-local"
if command -v shasum >/dev/null 2>&1; then
  actual_sha="$(shasum -a 256 "$tmp_dir/smarttex-local" | awk '{print $1}')"
else
  actual_sha="$(sha256sum "$tmp_dir/smarttex-local" | awk '{print $1}')"
fi
if [[ "$actual_sha" != "$asset_sha" ]]; then
  echo "Checksum mismatch for SmartTeX Local Agent" >&2
  echo "expected: $asset_sha" >&2
  echo "actual:   $actual_sha" >&2
  exit 1
fi

mkdir -p "$bin_dir"
install_path="$bin_dir/smarttex-local"
install -m 0755 "$tmp_dir/smarttex-local" "$install_path"

path_hint=""
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    shell_name="$(basename "${SHELL:-}")"
    profile=""
    case "$shell_name" in
      zsh) profile="$HOME/.zshrc" ;;
      bash) profile="$HOME/.bashrc" ;;
    esac
    if [[ -n "$profile" ]]; then
      mkdir -p "$(dirname "$profile")"
      touch "$profile"
      if ! grep -Fq "$bin_dir" "$profile"; then
        {
          echo ""
          echo "# SmartTeX Local Agent"
          echo "export PATH=\"$bin_dir:\$PATH\""
        } >> "$profile"
        path_hint="Added $bin_dir to PATH in $profile. Restart your terminal or run: source $profile"
      fi
    fi
    ;;
esac

echo
echo "Installed: $install_path"
echo "Version: $("$install_path" version)"
if [[ -n "$path_hint" ]]; then
  echo "$path_hint"
fi
echo
echo "Next:"
echo "  smarttex-local login --server ${server%/}"
echo "  smarttex-local serve"
EOF
chmod 0755 "$OUT_DIR/install.sh"

cat > "$OUT_DIR/install.ps1" <<'EOF'
$ErrorActionPreference = "Stop"

$Channel = if ($env:SMARTTEX_LOCAL_UPDATE_CHANNEL) { $env:SMARTTEX_LOCAL_UPDATE_CHANNEL } else { "stable" }
$InstallDir = if ($env:SMARTTEX_LOCAL_BIN_DIR) { $env:SMARTTEX_LOCAL_BIN_DIR } else { Join-Path $env:LOCALAPPDATA "SmartTeX\bin" }
$Server = if ($env:SMARTTEX_SERVER) { $env:SMARTTEX_SERVER.TrimEnd("/") } else { "https://smart-tex.pp.ua" }
$BaseUrl = "$Server/static/local-agent/$Channel"
$ManifestUrl = "$BaseUrl/manifest.json"

$ArchRaw = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
switch ($ArchRaw) {
  "arm64" { $Arch = "arm64" }
  "x64" { $Arch = "amd64" }
  "x86" { throw "SmartTeX Local Agent does not ship a 32-bit Windows binary." }
  default { throw "Unsupported Windows architecture: $ArchRaw" }
}

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("smarttex-local-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
try {
  $ManifestPath = Join-Path $TempDir "manifest.json"
  Write-Host "Fetching SmartTeX Local Agent manifest: $ManifestUrl"
  Invoke-WebRequest -Uri $ManifestUrl -OutFile $ManifestPath
  $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
  $Asset = $Manifest.assets | Where-Object { $_.os -eq "windows" -and $_.arch -eq $Arch } | Select-Object -First 1
  if (-not $Asset) {
    throw "No SmartTeX Local Agent binary for windows/$Arch"
  }

  $AssetUrl = [string]$Asset.url
  if (-not ($AssetUrl.StartsWith("http://") -or $AssetUrl.StartsWith("https://"))) {
    $AssetUrl = "$Server$AssetUrl"
  }
  $DownloadPath = Join-Path $TempDir "smarttex-local.exe"
  Write-Host "Downloading SmartTeX Local Agent $($Manifest.version): $AssetUrl"
  Invoke-WebRequest -Uri $AssetUrl -OutFile $DownloadPath

  $ActualSha = (Get-FileHash -Algorithm SHA256 -Path $DownloadPath).Hash.ToLowerInvariant()
  $ExpectedSha = ([string]$Asset.sha256).ToLowerInvariant()
  if ($ActualSha -ne $ExpectedSha) {
    throw "Checksum mismatch. Expected $ExpectedSha, got $ActualSha"
  }

  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  $InstallPath = Join-Path $InstallDir "smarttex-local.exe"
  Copy-Item -Force $DownloadPath $InstallPath

  $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $PathParts = @()
  if ($UserPath) { $PathParts = $UserPath.Split(";") | Where-Object { $_ } }
  $AlreadyInPath = $PathParts | Where-Object { $_.TrimEnd("\") -ieq $InstallDir.TrimEnd("\") }
  if (-not $AlreadyInPath) {
    $NextPath = if ($UserPath) { "$UserPath;$InstallDir" } else { $InstallDir }
    [Environment]::SetEnvironmentVariable("Path", $NextPath, "User")
    $env:Path = "$env:Path;$InstallDir"
    Write-Host "Added $InstallDir to your user PATH. Restart PowerShell if smarttex-local is not found."
  }

  Write-Host ""
  Write-Host "Installed: $InstallPath"
  & $InstallPath version
  Write-Host ""
  Write-Host "Next:"
  Write-Host "  smarttex-local login --server $Server"
  Write-Host "  smarttex-local serve"
} finally {
  Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
}
EOF

echo "Wrote $OUT_DIR/manifest.json"
echo "Wrote $OUT_DIR/install.sh"
echo "Wrote $OUT_DIR/install.ps1"
