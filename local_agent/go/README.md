# SmartTeX Local Agent

Local runtime for compiling Typst projects and running Tinymist features on the
user's machine instead of the SmartTeX server.

The server remains the source of truth for files, versions, AI proposals,
locks, and permissions. The local agent pulls compile-ready snapshots, runs
local tools, and uploads PDF/log artifacts back to SmartTeX.

## Build

```bash
cd local_agent/go
go build -o ../../bin/smarttex-local ./cmd/smarttex-local-go
```

## Install

For a one-command local install:

```bash
local_agent/go/scripts/install-local-agent.sh --server https://smart-tex.pp.ua
```

The installer builds `smarttex-local`, generates a local bridge secret, writes
`~/.smarttex-local/local-agent.env`, and creates a user service on macOS
(`launchd`) or Linux (`systemd`). Use `--start` if you want it to start the
service immediately.

After install, run OAuth once as the same OS user:

```bash
~/.local/bin/smarttex-local login --server https://smart-tex.pp.ua
set -a
source ~/.smarttex-local/local-agent.env
set +a
~/.local/bin/smarttex-local doctor --server "$SMARTTEX_SERVER"
```

## CLI

```bash
export SMARTTEX_SERVER="http://localhost:8000"

./bin/smarttex-local login
./bin/smarttex-local login --serve

./bin/smarttex-local doctor
./bin/smarttex-local projects
./bin/smarttex-local compile --project 142
```

`login` performs a browser OAuth flow with PKCE and stores an access token plus
a rotating refresh token in `~/.smarttex-local/config.json`. It also creates a
persistent local bridge secret, so you do not need to generate
`SMARTTEX_LOCAL_SECRET` by hand. Use `login --serve` to start the bridge
immediately after OAuth in the same terminal. Long-running `serve` processes
refresh automatically before server calls. For development only,
`SMARTTEX_TOKEN` or `--token` can still override the saved OAuth session.

## Web Editor Integration

Start the local bridge:

```bash
export SMARTTEX_SERVER="http://localhost:8000"

./bin/smarttex-local serve
```

The bridge secret is separate from OAuth: OAuth authorizes the agent against
SmartTeX, while the secret prevents arbitrary browser pages from calling your
localhost agent.

Before leaving the agent running, use:

```bash
./bin/smarttex-local doctor
```

`doctor` verifies the saved token/server, `typst`, `tinymist`, and whether the
bridge secret is available.

For local Typst web preview, `tinymist` must be installed on the host:

```bash
# example
cargo install --git https://github.com/Myriad-Dreamin/tinymist tinymist

# or point to a downloaded binary
./bin/smarttex-local serve --tinymist-bin /path/to/tinymist
```

Then use the `Local` status-bar control in the editor. It opens a small setup
panel where you can enter the agent URL, paste the bridge secret, test
`/v1/health`, and enable or disable Local mode for the current project.

After that, the editor stores Local mode on the server for this project. Normal
SmartTeX compile requests, including MCP `compile_project`, create a local
compile job. The agent polls for that job, compiles locally, and uploads the
result. Preview and LSP connect to the localhost bridge directly from the
browser.

To disable Local mode for MCP/server compiles, use the editor `Local` control or:

```bash
curl -X PUT "$SMARTTEX_SERVER/api/projects/142/local-runtime/" \
  -H "Authorization: Bearer $SMARTTEX_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"enabled":false}'
```

## Running As A Service

The agent handles `SIGINT`/`SIGTERM` and shuts down the HTTP bridge cleanly.
For long-running use, wrap it in your OS service manager with these env vars:

```bash
SMARTTEX_SERVER="https://smart-tex.pp.ua"
SMARTTEX_LOCAL_WORKSPACE="$HOME/.smarttex-local"
```

Then run:

```bash
./bin/smarttex-local serve
```

The server-side Local setting is per-project. The agent can serve multiple
enabled projects for the authenticated user and polls SmartTeX for queued jobs.

Example service templates live in:

- `packaging/launchd/com.smarttex.local-agent.plist.example` for macOS.
- `packaging/systemd/smarttex-local.service.example` for Linux user services.

Copy the example, replace paths, and run `smarttex-local doctor` before
enabling the service. The OAuth token and bridge secret live in
`~/.smarttex-local/config.json`, so run `smarttex-local login` once as the same
OS user that will own the service.

The installer can generate these service files for you:

```bash
local_agent/go/scripts/install-local-agent.sh --start
```

## Updates

The agent can update itself from a server-published manifest:

```bash
smarttex-local update --server https://smart-tex.pp.ua
```

`update` downloads `/static/local-agent/<channel>/manifest.json`, selects the
binary for the current OS/architecture, verifies SHA-256, and atomically
replaces the current executable. Use `--install-path` when updating a specific
binary path, and `--channel beta` for non-stable release channels.

Typst and Tinymist can be managed from the same manifest:

```bash
smarttex-local toolchain install --server https://smart-tex.pp.ua
smarttex-local toolchain status
```

`toolchain install` downloads `toolchains[]` entries for the current
OS/architecture, verifies SHA-256, installs them into
`~/.smarttex-local/toolchains/<channel>/...`, and stores the selected
`typst_bin` / `tinymist_bin` paths in `~/.smarttex-local/config.json`.
After that, `compile`, `serve`, and `doctor` use the managed binaries by
default. `TYPST_BINARY`, `TINYMIST_BIN`, `--typst-bin`, and `--tinymist-bin`
still override this for development.

To build the static update bundle for deployment:

```bash
SMARTTEX_LOCAL_UPDATE_CHANNEL=stable local_agent/go/scripts/build-release-assets.sh
```

The script writes precompiled binaries and `manifest.json` to
`projects/static/local-agent/stable/`. The GitHub Actions workflow
`.github/workflows/smarttex-local-agent-release.yml` builds the same bundle as
a release artifact.

To include toolchain assets in the generated manifest, pass a JSON file:

```bash
SMARTTEX_TOOLCHAIN_MANIFEST=toolchains.json \
  SMARTTEX_LOCAL_UPDATE_CHANNEL=stable \
  local_agent/go/scripts/build-release-assets.sh
```

Example `toolchains.json`:

```json
{
  "toolchains": [
    {
      "tool": "typst",
      "version": "0.13.1",
      "os": "darwin",
      "arch": "arm64",
      "url": "/static/local-agent/stable/toolchains/typst-darwin-arm64",
      "sha256": "...",
      "executable": "typst"
    }
  ]
}
```

Server operators can disable all local runtime traffic with:

```bash
LOCAL_RUNTIME_ENABLED=False
```

When disabled, project editing and regular server compilation continue to work,
but Local mode reports `unavailable`, agent heartbeat/job claim endpoints reject
requests, and compile requests fall back to server compilation.

## Current Scope

- Typst projects only.
- Server-side Local mode is project-scoped and uses a persistent job queue.
- The agent sends heartbeat and polls for queued compile jobs.
- Compile, preview, and LSP use separate local workspaces to avoid clobbering
  each other.
- Pulls a full compile-support ZIP for each compile/preview/LSP workspace sync.
- Runs `typst compile --root . <main-file> .smarttex/main.pdf`.
- Uploads PDF/log via multipart to `/api/projects/<id>/compile/local-result/`.
- Local `tinymist preview` can be embedded when `tinymist` exists on the host.
- Local `tinymist lsp` is exposed through `/v1/lsp` using the same WebSocket
  envelope as the server Tinymist bridge.
- `doctor` checks the local toolchain and SmartTeX authentication before use.
- `update` installs precompiled binaries from the SmartTeX static release
  manifest with checksum verification.
- `toolchain install` can pin Typst/Tinymist to server-published versions
  without bundling them inside the agent binary.
