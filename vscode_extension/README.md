# SmartTeX VS Code Extension

Edit SmartTeX projects as normal local files while `smarttex-local` keeps the
server workspace locked, synchronized, compiled, and connected to local Typst
preview features.

## Requirements

- Install the SmartTeX local agent (`smarttex-local`).
- Run `SmartTeX: Login` once from the command palette.

## Production Install

The SmartTeX local-agent release bundle includes this extension as a signed-by
checksum `.vsix` asset in the same static channel manifest as the agent
binaries.

macOS/Linux:

```bash
curl -fsSL https://smart-tex.pp.ua/static/local-agent/stable/install.sh | SMARTTEX_SERVER='https://smart-tex.pp.ua' bash
smarttex-local login --serve --server https://smart-tex.pp.ua
```

Windows PowerShell:

```powershell
$env:SMARTTEX_SERVER='https://smart-tex.pp.ua'; iwr -useb https://smart-tex.pp.ua/static/local-agent/stable/install.ps1 | iex
smarttex-local login --serve --server https://smart-tex.pp.ua
```

The installer downloads the agent and VSIX from
`/static/local-agent/<channel>/manifest.json`, verifies SHA-256 checksums, adds
the agent to PATH, and installs the extension with `code --install-extension`
when the VS Code CLI is available. If `code` is not available, it prints the
VSIX URL so the user can install it via `Extensions: Install from VSIX...`.

## Dev Testing Without Packaging

1. Open this folder in VS Code.
2. Press `F5` and choose `Run SmartTeX Extension` if VS Code asks.
3. Use the command palette in the new `Extension Development Host` window, not in the launcher window.

No `.vsix`, npm install, or build step is required for local development.
The checked-in `.vscode/settings.json` is intentionally local-dev oriented:

- `smarttex.serverUrl` points to `http://localhost:8000`.
- `smarttex.localAgentPath` points to the repo wrapper at `../local_agent/go/scripts/smarttex-local-dev.sh`.
- `smarttex.workspaceRoot` uses `~/.smarttex-local-dev` so testing does not touch the normal local-agent workspace.
- `smarttex.autoWatchAfterOpen` is disabled so testing does not spawn a long-running terminal by default.
- `smarttex.syncOnSave` is enabled, so saving a file runs a quiet one-shot workspace sync.
- `smarttex.autoSaveAndSync` is enabled, so changed files are saved and synced automatically after a short debounce.

## Main Flow

1. Run `SmartTeX: Open Project Locally`.
2. Enter the SmartTeX project id.
3. The extension asks `smarttex-local` to pull the project and claim a local workspace lease.
4. The folder opens in VS Code.
5. By default, file changes are auto-saved and synced back to SmartTeX after a short debounce. If `smarttex.autoWatchAfterOpen` is enabled, a terminal starts `smarttex-local workspace watch` instead.

The web editor becomes read-only while the local workspace lease is active. Use
`SmartTeX: Release Local Workspace` when you are done, or switch back from the web
editor using its local-workspace lock overlay.

## Commands

- `SmartTeX: Login` starts OAuth login through `smarttex-local`.
- `SmartTeX: Open Project Locally` pulls a project, claims the edit lease, and opens the workspace.
- `SmartTeX: Add Annotation` creates an annotation for the current selection or line. Shortcut: `Cmd/Ctrl+Shift+A`.
- `SmartTeX: Refresh Annotations` reloads the SmartTeX annotations tree.
- `SmartTeX: Open Local Preview` opens the local Tinymist preview in a VS Code editor tab.
- `SmartTeX: Start Local Agent` starts `smarttex-local serve` in a VS Code terminal.
- `SmartTeX: Watch and Sync Workspace` starts continuous saved-file sync in a terminal.
- `SmartTeX: Sync Workspace Now` uploads local changes once.
- `SmartTeX: Pull Server Snapshot` refreshes the local files from the server.
- `SmartTeX: Release Local Workspace` releases the edit lease.
- `SmartTeX: Workspace Status` prints local/server state to the SmartTeX output panel.
- `SmartTeX: Compile Project` runs local compile through the agent.
- `SmartTeX: Open Web Editor` opens the matching SmartTeX web project.

## Annotations

Open the SmartTeX activity bar item and use the `Annotations` tree to review notes.
Click an annotation to jump to its file and line. Inline actions can mark it done,
dismiss it, or keep an AI draft as a regular open annotation.

By default the extension shows only active annotations: `ai_draft`, `open`, and
`in_progress`. Enable `smarttex.showResolvedAnnotations` to also show `done` and
`dismissed` annotations in the tree and editor.

## Preview

Run `SmartTeX: Start Local Agent` if the local bridge is not already running, then
run `SmartTeX: Open Local Preview`. The preview uses the local agent bridge at
`smarttex.localBridgeUrl` and the saved OAuth/local bridge config.

## Settings

- `smarttex.localAgentPath`: path to `smarttex-local`.
- `smarttex.serverUrl`: SmartTeX server URL.
- `smarttex.workspaceRoot`: local root for pulled project workspaces.
- `smarttex.autoWatchAfterOpen`: start watch sync after opening a project.
- `smarttex.syncOnSave`: sync the current local workspace whenever a file is saved.
- `smarttex.autoSaveAndSync`: automatically save changed workspace files and sync them.
- `smarttex.autoSaveDebounceMs`: delay before auto-save and sync.
- `smarttex.annotationRefreshIntervalMs`: refresh interval for realtime-ish annotation updates.
- `smarttex.annotationCodeLens`: show clickable inline annotation actions in the editor.
- `smarttex.showResolvedAnnotations`: include done/dismissed annotations in VS Code.
- `smarttex.localBridgeUrl`: local bridge URL for Typst preview.
