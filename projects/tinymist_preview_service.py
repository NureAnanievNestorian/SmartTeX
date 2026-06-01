"""
Tinymist web preview sidecar manager.

Runs `tinymist preview` per (project_id, user_id) and exposes a local HTTP server
that Django can proxy into the editor iframe.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
from collections import deque
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

SESSION_IDLE_TIMEOUT: int = 600  # seconds
DEFAULT_PREVIEW_DATA_PLANE_PORT: int = 23625
DEFAULT_PREVIEW_CONTROL_PLANE_PORT: int = 23626
_CONTROL_HOST_RE = re.compile(r"Control panel server listening on:\s+[^:]+:(?P<port>\d+)")


def _tinymist_bin() -> str:
    return str(getattr(settings, "TINYMIST_BIN", os.getenv("TINYMIST_BIN", "tinymist")) or "tinymist")


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_until_ready(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except Exception:
            await asyncio.sleep(0.15)
    raise RuntimeError("Tinymist preview did not start in time")


def _normalize_invert_colors(value: str | None) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"auto", "always", "never"}:
        return normalized
    return "auto"


class TinymistPreviewSession:
    def __init__(self, project_id: int, user_id: int, project_root: Path, main_file: Path, invert_colors: str = "auto"):
        self.project_id = project_id
        self.user_id = user_id
        self.project_root = project_root
        self.main_file = main_file
        self.invert_colors = _normalize_invert_colors(invert_colors)
        self.port = _reserve_local_port()
        self.data_plane_port = DEFAULT_PREVIEW_DATA_PLANE_PORT
        self.control_plane_port = DEFAULT_PREVIEW_CONTROL_PLANE_PORT
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._last_activity = time.monotonic()
        self._stderr_lines: deque[str] = deque(maxlen=40)

    async def start(self) -> None:
        if self._proc and self._proc.returncode is None:
            return
        cmd = [
            _tinymist_bin(),
            "preview",
            str(self.main_file),
            f"--root={self.project_root}",
            "--partial-rendering=true",
            f"--host=127.0.0.1:{self.port}",
            f"--invert-colors={self.invert_colors}",
            "--no-open",
        ]
        logger.info(
            "tinymist preview: starting project=%s user=%s port=%s",
            self.project_id,
            self.user_id,
            self.port,
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project_root),
        )
        self._stderr_task = asyncio.create_task(self._stderr_reader())
        try:
          await _wait_until_ready(self.port)
        except Exception as exc:
          if self._proc and self._proc.returncode is not None:
            tail = self.stderr_tail()
            raise RuntimeError(f"Tinymist preview exited early ({self._proc.returncode}). {tail}".strip()) from exc
          tail = self.stderr_tail()
          raise RuntimeError(f"Tinymist preview did not start in time. {tail}".strip()) from exc
        logger.info(
            "tinymist preview: ready project=%s user=%s port=%s",
            self.project_id,
            self.user_id,
            self.port,
        )

    async def stop(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        logger.info("tinymist preview: stopped project=%s user=%s", self.project_id, self.user_id)

    async def restart(self) -> None:
        await self.stop()
        self.port = _reserve_local_port()
        await self.start()

    async def _stderr_reader(self) -> None:
        while True:
            try:
                if not self._proc or not self._proc.stderr:
                    return
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip()
                self._stderr_lines.append(text)
                match = _CONTROL_HOST_RE.search(text)
                if match:
                    try:
                        self.control_plane_port = int(match.group("port"))
                    except (TypeError, ValueError):
                        pass
                logger.info("tinymist preview stderr: %s", text)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    def stderr_tail(self) -> str:
        if not self._stderr_lines:
            return ""
        return "stderr: " + " | ".join(self._stderr_lines)


_sessions: dict[tuple[int, int], TinymistPreviewSession] = {}
_registry_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def get_or_create_preview_session(project_id: int, user_id: int, invert_colors: str | None = None) -> TinymistPreviewSession:
    key = (project_id, user_id)
    async with _lock():
        session = _sessions.get(key)
        next_invert = _normalize_invert_colors(invert_colors) if invert_colors is not None else None
        if session and session.is_alive() and (next_invert is None or session.invert_colors == next_invert):
            session.touch()
            return session
        if session and session.is_alive() and next_invert is not None and session.invert_colors != next_invert:
            await session.stop()

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
        session = TinymistPreviewSession(project_id, user_id, root, main, invert_colors=next_invert or "auto")
        await session.start()
        _sessions[key] = session
        return session


async def get_latest_preview_session_for_user(user_id: int) -> TinymistPreviewSession | None:
    async with _lock():
        alive = [session for (_project_id, uid), session in _sessions.items() if uid == user_id and session.is_alive()]
        if not alive:
            return None
        session = max(alive, key=lambda item: item._last_activity)
        session.touch()
        return session


async def restart_preview_session(project_id: int, user_id: int, invert_colors: str | None = None) -> TinymistPreviewSession:
    key = (project_id, user_id)
    async with _lock():
        session = _sessions.get(key)
        if session is None:
            session = await get_or_create_preview_session(project_id, user_id, invert_colors=invert_colors)
            return session
        session.invert_colors = _normalize_invert_colors(invert_colors or session.invert_colors)
    await session.restart()
    return session


async def close_preview_session(project_id: int, user_id: int) -> None:
    key = (project_id, user_id)
    async with _lock():
        session = _sessions.pop(key, None)
    if session:
        await session.stop()


async def reap_idle_preview_sessions() -> None:
    async with _lock():
        expired = [k for k, s in list(_sessions.items()) if s.idle_seconds() > SESSION_IDLE_TIMEOUT]
        sessions = [_sessions.pop(k) for k in expired]
    for session in sessions:
        logger.info(
            "tinymist preview: reaping idle session project=%s user=%s",
            session.project_id,
            session.user_id,
        )
        await session.stop()
