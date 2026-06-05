#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install SmartTeX Local Agent.

Usage:
  install-local-agent.sh [options]

Options:
  --server URL          SmartTeX server URL. Default: https://smart-tex.pp.ua
  --bin-dir DIR         Install binary into DIR. Default: ~/.local/bin
  --workspace DIR       Local workspace root. Default: ~/.smarttex-local
  --listen ADDR         Local bridge listen address. Default: 127.0.0.1:8765
  --secret VALUE        Local bridge secret. Default: generated
  --tinymist-bin PATH   tinymist executable path. Default: auto-detected or tinymist
  --no-service          Build binary/env only; do not create launchd/systemd service
  --start               Start the generated service after installation
  -h, --help            Show this help

Environment overrides:
  SMARTTEX_SERVER, SMARTTEX_LOCAL_SECRET, SMARTTEX_LOCAL_WORKSPACE,
  SMARTTEX_LOCAL_LISTEN, TINYMIST_BIN
EOF
}

quote_env() {
  printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agent_dir="$(cd "$script_dir/.." && pwd)"

server="${SMARTTEX_SERVER:-https://smart-tex.pp.ua}"
bin_dir="${SMARTTEX_LOCAL_BIN_DIR:-$HOME/.local/bin}"
workspace="${SMARTTEX_LOCAL_WORKSPACE:-$HOME/.smarttex-local}"
listen="${SMARTTEX_LOCAL_LISTEN:-127.0.0.1:8765}"
secret="${SMARTTEX_LOCAL_SECRET:-}"
tinymist_bin="${TINYMIST_BIN:-}"
install_service=1
start_service=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server)
      server="${2:?--server requires a value}"
      shift 2
      ;;
    --bin-dir)
      bin_dir="${2:?--bin-dir requires a value}"
      shift 2
      ;;
    --workspace)
      workspace="${2:?--workspace requires a value}"
      shift 2
      ;;
    --listen)
      listen="${2:?--listen requires a value}"
      shift 2
      ;;
    --secret)
      secret="${2:?--secret requires a value}"
      shift 2
      ;;
    --tinymist-bin)
      tinymist_bin="${2:?--tinymist-bin requires a value}"
      shift 2
      ;;
    --no-service)
      install_service=0
      shift
      ;;
    --start)
      start_service=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$secret" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    secret="$(openssl rand -hex 24)"
  else
    secret="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  fi
fi

if [[ -z "$tinymist_bin" ]]; then
  if command -v tinymist >/dev/null 2>&1; then
    tinymist_bin="$(command -v tinymist)"
  else
    tinymist_bin="tinymist"
  fi
fi

mkdir -p "$bin_dir" "$workspace"
binary="$bin_dir/smarttex-local"

echo "Building SmartTeX Local Agent..."
(cd "$agent_dir" && go build -o "$binary" ./cmd/smarttex-local-go)
chmod 0755 "$binary"

env_file="$workspace/local-agent.env"
umask 077
cat >"$env_file" <<EOF
SMARTTEX_SERVER=$(quote_env "$server")
SMARTTEX_LOCAL_SECRET=$(quote_env "$secret")
SMARTTEX_LOCAL_WORKSPACE=$(quote_env "$workspace")
SMARTTEX_LOCAL_LISTEN=$(quote_env "$listen")
TINYMIST_BIN=$(quote_env "$tinymist_bin")
EOF
chmod 0600 "$env_file"

config_file="$workspace/config.json"
SMARTTEX_INSTALL_SERVER="$server" SMARTTEX_INSTALL_SECRET="$secret" SMARTTEX_INSTALL_CONFIG="$config_file" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["SMARTTEX_INSTALL_CONFIG"])
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
data.setdefault("server", os.environ["SMARTTEX_INSTALL_SERVER"])
data.setdefault("bridge_secret", os.environ["SMARTTEX_INSTALL_SECRET"])
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
path.chmod(0o600)
PY

service_path=""
service_kind=""

if [[ "$install_service" -eq 1 ]]; then
  case "$(uname -s)" in
    Darwin)
      service_kind="launchd"
      mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
      service_path="$HOME/Library/LaunchAgents/com.smarttex.local-agent.plist"
      cat >"$service_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.smarttex.local-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$binary</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SMARTTEX_SERVER</key>
    <string>$server</string>
    <key>SMARTTEX_LOCAL_SECRET</key>
    <string>$secret</string>
    <key>SMARTTEX_LOCAL_WORKSPACE</key>
    <string>$workspace</string>
    <key>SMARTTEX_LOCAL_LISTEN</key>
    <string>$listen</string>
    <key>TINYMIST_BIN</key>
    <string>$tinymist_bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/smarttex-local-agent.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/smarttex-local-agent.err.log</string>
</dict>
</plist>
EOF
      if [[ "$start_service" -eq 1 ]]; then
        launchctl unload "$service_path" >/dev/null 2>&1 || true
        launchctl load "$service_path"
      fi
      ;;
    Linux)
      service_kind="systemd"
      mkdir -p "$HOME/.config/systemd/user"
      service_path="$HOME/.config/systemd/user/smarttex-local.service"
      cat >"$service_path" <<EOF
[Unit]
Description=SmartTeX Local Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$(quote_env "$binary") serve
EnvironmentFile=$(quote_env "$env_file")
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload
      systemctl --user enable smarttex-local.service
      if [[ "$start_service" -eq 1 ]]; then
        systemctl --user restart smarttex-local.service
      fi
      ;;
    *)
      echo "Service install is not supported on this OS; binary/env were installed." >&2
      install_service=0
      ;;
  esac
fi

cat <<EOF

SmartTeX Local Agent installed.

Binary:
  $binary

Environment:
  $env_file

Config:
  $config_file

Bridge URL for the editor:
  http://$listen

Bridge secret:
  $secret
EOF

if [[ "$install_service" -eq 1 ]]; then
  cat <<EOF

Service:
  $service_kind -> $service_path
EOF
fi

cat <<EOF

Next:
  1. Run: $(quote_env "$binary") login --server $(quote_env "$server")
  2. Run: $(quote_env "$binary") doctor --server $(quote_env "$server")
EOF

next_step=3
if [[ "$install_service" -eq 1 && "$start_service" -eq 0 ]]; then
  if [[ "$service_kind" == "launchd" ]]; then
    echo "  $next_step. Start: launchctl load $(quote_env "$service_path")"
    next_step=$((next_step + 1))
  elif [[ "$service_kind" == "systemd" ]]; then
    echo "  $next_step. Start: systemctl --user start smarttex-local.service"
    next_step=$((next_step + 1))
  fi
elif [[ "$start_service" -eq 1 ]]; then
  echo "  $next_step. Service start requested."
  next_step=$((next_step + 1))
fi

cat <<EOF
  $next_step. Open SmartTeX editor -> Local -> paste URL and secret -> Test -> Enable.

EOF
