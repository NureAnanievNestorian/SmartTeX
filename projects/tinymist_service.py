"""
Tinymist LSP process manager.

One tinymist process per (project_id, user_id).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def _tinymist_lsp_settings() -> dict:
    # Mirror the documented editor settings for non-VSCode clients.
    return {
        "previewFeature": "enable",
        "projectResolution": "lockDatabase",
        "preview": {
            "scrollSync": "onSelectionChange",
            "cursorIndicator": True,
            "refresh": "onType",
            "partialRendering": True,
            "invertColors": "auto",
        },
    }

_HEADER_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)
SESSION_IDLE_TIMEOUT: int = int(getattr(settings, "TINYMIST_SESSION_IDLE_TIMEOUT", 600))


def _debug_enabled() -> bool:
    return bool(getattr(settings, "TINYMIST_DEBUG", False))


def _tinymist_bin() -> str:
    return str(getattr(settings, "TINYMIST_BIN", os.getenv("TINYMIST_BIN", "tinymist")) or "tinymist")


class TinymistSession:
    def __init__(self, project_id: int, user_id: int, project_root: Path, main_file: Path):
        self.project_id = project_id
        self.user_id = user_id
        self.project_root = project_root
        self.main_file = main_file

        self._proc: asyncio.subprocess.Process | None = None
        self._send_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self.outbox: asyncio.Queue[dict] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.initialized = False
        self._last_activity = time.monotonic()
        self.semantic_tokens_legend: dict = {"tokenTypes": [], "tokenModifiers": []}

        # API (MCP) request support — separate ID space to avoid conflicts with browser requests
        self._api_next_id = 10_000_000
        self._api_pending: dict[int, asyncio.Future] = {}
        self._diag_queues: dict[str, list[asyncio.Queue]] = {}
        self._open_files: set[str] = set()  # URIs opened via api_open_file
        self._open_file_versions: dict[str, int] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._proc and self._proc.returncode is None:
            return
        cmd = [_tinymist_bin(), "lsp"]
        logger.info("tinymist: starting project=%s user=%s", self.project_id, self.user_id)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project_root),
        )
        self._reader_task = asyncio.create_task(self._stdout_reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())
        await self._handshake()
        self.initialized = True
        logger.info("tinymist: ready project=%s user=%s", self.project_id, self.user_id)

    async def stop(self) -> None:
        self.initialized = False
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._reader_task = None
        self._stderr_task = None
        if self._proc:
            try:
                await self._write_msg({"jsonrpc": "2.0", "method": "exit"})
            except Exception:
                pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        logger.info("tinymist: stopped project=%s user=%s", self.project_id, self.user_id)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    # ── LSP framing ──────────────────────────────────────────────────────────

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        return b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload

    async def _write_msg(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False).encode()
        async with self._send_lock:
            if not self._proc or not self._proc.stdin:
                raise RuntimeError("tinymist not running")
            if _debug_enabled():
                logger.info(
                    "tinymist -> lsp project=%s user=%s method=%s id=%s",
                    self.project_id,
                    self.user_id,
                    message.get("method"),
                    message.get("id"),
                )
            self._proc.stdin.write(self._frame(payload))
            await self._proc.stdin.drain()

    # ── Readers ──────────────────────────────────────────────────────────────

    async def _stdout_reader(self) -> None:
        buf = b""
        while True:
            try:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = _HEADER_RE.search(buf)
                    if not m:
                        break
                    header_end = buf.find(b"\r\n\r\n", m.start())
                    if header_end == -1:
                        break
                    content_length = int(m.group(1))
                    body_start = header_end + 4
                    if len(buf) < body_start + content_length:
                        break
                    body = buf[body_start : body_start + content_length]
                    buf = buf[body_start + content_length :]
                    try:
                        self._dispatch(json.loads(body))
                    except json.JSONDecodeError as exc:
                        logger.warning("tinymist: bad JSON from stdout: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("tinymist: stdout reader error: %s", exc)
                break

    async def _stderr_reader(self) -> None:
        while True:
            try:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug("tinymist stderr: %s", line.decode(errors="replace").rstrip())
            except asyncio.CancelledError:
                raise
            except Exception:
                break

    def _dispatch(self, msg: dict) -> None:
        if _debug_enabled():
            logger.info(
                "tinymist <- lsp project=%s user=%s method=%s id=%s has_result=%s has_error=%s",
                self.project_id,
                self.user_id,
                msg.get("method"),
                msg.get("id"),
                "result" in msg,
                "error" in msg,
            )
        msg_id = msg.get("id")
        method = msg.get("method")

        # During handshake: route responses to _pending futures
        if not self.initialized and msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return

        # API requests: route to api_pending (never go to outbox)
        if msg_id is not None and msg_id in self._api_pending:
            fut = self._api_pending.pop(msg_id)
            if not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return

        # Diagnostic notifications: push to waiting queues
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri = params.get("uri", "")
            for q in list(self._diag_queues.get(uri, [])):
                q.put_nowait(params)

        self.outbox.put_nowait(msg)

    # ── LSP initialize handshake ─────────────────────────────────────────────

    async def _request(self, method: str, params: Any, timeout: float = 30.0) -> Any:
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def _handshake(self) -> None:
        root_uri = self.project_root.as_uri()
        settings_payload = _tinymist_lsp_settings()
        result = await self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "completion": {
                        "completionItem": {
                            "snippetSupport": True,
                            "labelDetailsSupport": True,
                        },
                        "contextSupport": True,
                    },
                    "signatureHelp": {
                        "signatureInformation": {
                            "documentationFormat": ["markdown", "plaintext"],
                            "parameterInformation": {"labelOffsetSupport": True},
                            "activeParameterSupport": True,
                        }
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {},
                    "references": {},
                    "documentSymbol": {},
                    "documentLink": {},
                    "rename": {"prepareSupport": False},
                    "publishDiagnostics": {"relatedInformation": True},
                    "formatting": {},
                    "foldingRange": {"lineFoldingOnly": False},
                    "semanticTokens": {
                        "formats": ["relative"],
                        "requests": {"full": True},
                        "multilineTokenSupport": False,
                        "overlappingTokenSupport": False,
                        "tokenTypes": [
                            "namespace", "type", "class", "enum", "interface",
                            "struct", "typeParameter", "parameter", "variable",
                            "property", "enumMember", "event", "function",
                            "method", "macro", "keyword", "modifier",
                            "comment", "string", "number", "regexp",
                            "operator", "decorator",
                        ],
                        "tokenModifiers": [
                            "declaration", "definition", "readonly",
                            "static", "deprecated", "abstract",
                            "async", "modification", "documentation",
                            "defaultLibrary",
                        ],
                    },
                },
                "workspace": {
                    "workspaceFolders": True,
                    "symbol": {},
                },
            },
            "workspaceFolders": [{"uri": root_uri, "name": f"project-{self.project_id}"}],
            "initializationOptions": {
                "settings": settings_payload,
            },
        })
        # Store the semantic token legend returned by the server
        caps = (result or {}).get("capabilities", {})
        st_provider = caps.get("semanticTokensProvider", {})
        legend = st_provider.get("legend", {})
        self.semantic_tokens_legend = {
            "tokenTypes": legend.get("tokenTypes", []),
            "tokenModifiers": legend.get("tokenModifiers", []),
        }
        await self._write_msg({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        await self._write_msg({
            "jsonrpc": "2.0",
            "method": "workspace/didChangeConfiguration",
            "params": {"settings": settings_payload},
        })

    # ── Public API ───────────────────────────────────────────────────────────

    async def send_raw(self, message: dict) -> None:
        """Forward a raw LSP message from the browser to tinymist stdin."""
        self.touch()
        await self._write_msg(message)

    async def api_request(self, method: str, params: Any, timeout: float = 10.0) -> Any:
        """Send an LSP request and wait for the response (used by API/MCP endpoints)."""
        req_id = self._api_next_id
        self._api_next_id += 1
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._api_pending[req_id] = fut
        await self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        self.touch()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._api_pending.pop(req_id, None)
            raise

    async def api_notify(self, method: str, params: Any) -> None:
        """Send an LSP notification (no response expected)."""
        await self._write_msg({"jsonrpc": "2.0", "method": method, "params": params})
        self.touch()

    async def api_open_file(self, uri: str, text: str, language_id: str = "typst") -> None:
        """Open or update a file in the LSP session for API use."""
        if uri in self._open_files:
            version = self._open_file_versions[uri] + 1
            self._open_file_versions[uri] = version
            await self.api_notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
        else:
            self._open_files.add(uri)
            self._open_file_versions[uri] = 1
            await self.api_notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": language_id, "version": 1, "text": text},
            })

    async def collect_diagnostics(self, uri: str, timeout: float = 3.0) -> list:
        """Wait for publishDiagnostics notification for a URI and return the diagnostics list."""
        q: asyncio.Queue = asyncio.Queue()
        self._diag_queues.setdefault(uri, []).append(q)
        try:
            params = await asyncio.wait_for(q.get(), timeout=timeout)
            return params.get("diagnostics", [])
        except asyncio.TimeoutError:
            return []
        finally:
            queues = self._diag_queues.get(uri, [])
            try:
                queues.remove(q)
            except ValueError:
                pass


# ── Session registry ─────────────────────────────────────────────────────────

_sessions: dict[tuple[int, int], TinymistSession] = {}
_api_sessions: dict[tuple[int, int], TinymistSession] = {}
_registry_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def get_or_create_session(project_id: int, user_id: int) -> TinymistSession:
    if not bool(getattr(settings, "TINYMIST_LSP_ENABLED", True)):
        raise RuntimeError("Tinymist LSP is disabled")
    key = (project_id, user_id)
    async with _lock():
        session = _sessions.get(key)
        if session and session.is_alive():
            session.touch()
            return session

        from asgiref.sync import sync_to_async

        from projects.models import Project
        from projects.services import main_source_filename, project_dir

        project = await sync_to_async(
            lambda: Project.objects.filter(id=project_id, owner_id=user_id).first()
        )()
        if project is None:
            raise PermissionError(f"project {project_id} not accessible for user {user_id}")

        root = project_dir(project)
        main = root / main_source_filename(project)
        session = TinymistSession(project_id, user_id, root, main)
        await session.start()
        _sessions[key] = session
        return session


async def get_or_create_api_session(project_id: int, user_id: int) -> TinymistSession:
    """Return a persistent tinymist session for API/MCP use, isolated from browser sessions."""
    if not bool(getattr(settings, "TINYMIST_LSP_ENABLED", True)):
        raise RuntimeError("Tinymist LSP is disabled")
    key = (project_id, user_id)
    async with _lock():
        session = _api_sessions.get(key)
        if session and session.is_alive():
            session.touch()
            return session

        from asgiref.sync import sync_to_async

        from projects.models import Project
        from projects.services import main_source_filename, project_dir

        project = await sync_to_async(
            lambda: Project.objects.filter(id=project_id, owner_id=user_id).first()
        )()
        if project is None:
            raise PermissionError(f"project {project_id} not accessible for user {user_id}")

        root = project_dir(project)
        main = root / main_source_filename(project)
        session = TinymistSession(project_id, user_id, root, main)
        await session.start()
        _api_sessions[key] = session
        return session


async def close_session(project_id: int, user_id: int) -> None:
    key = (project_id, user_id)
    async with _lock():
        session = _sessions.pop(key, None)
    if session:
        await session.stop()


async def close_api_session(project_id: int, user_id: int) -> None:
    key = (project_id, user_id)
    async with _lock():
        session = _api_sessions.pop(key, None)
    if session:
        await session.stop()


async def close_project_sessions(project_id: int) -> None:
    async with _lock():
        keys = [k for k in list(_sessions) if k[0] == project_id]
        sessions = [_sessions.pop(k) for k in keys]
        api_keys = [k for k in list(_api_sessions) if k[0] == project_id]
        sessions += [_api_sessions.pop(k) for k in api_keys]
    for session in sessions:
        await session.stop()


async def reap_idle_sessions() -> None:
    """Kill sessions that have been idle longer than SESSION_IDLE_TIMEOUT."""
    async with _lock():
        expired = [k for k, s in list(_sessions.items()) if s.idle_seconds() > SESSION_IDLE_TIMEOUT]
        sessions = [_sessions.pop(k) for k in expired]
        api_expired = [k for k, s in list(_api_sessions.items()) if s.idle_seconds() > SESSION_IDLE_TIMEOUT]
        sessions += [_api_sessions.pop(k) for k in api_expired]
    for session in sessions:
        logger.info(
            "tinymist: reaping idle session project=%s user=%s",
            session.project_id,
            session.user_id,
        )
        await session.stop()
