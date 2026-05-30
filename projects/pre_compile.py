from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY: list[type[PreCompileJob]] = []


@dataclass
class PreCompileResult:
    job: str
    success: bool
    files_processed: int = 0
    files_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class PreCompileJob(ABC):
    name: str = "unnamed"

    @abstractmethod
    def run(self, project: Any, workdir: Path) -> PreCompileResult:
        ...


def register(cls: type[PreCompileJob]) -> type[PreCompileJob]:
    _REGISTRY.append(cls)
    return cls


def run_pre_compile_jobs(project: Any) -> list[PreCompileResult]:
    from projects.services import ensure_project_dir
    workdir = ensure_project_dir(project)
    results: list[PreCompileResult] = []
    for job_cls in _REGISTRY:
        job = job_cls()
        try:
            result = job.run(project, workdir)
            results.append(result)
            if not result.success:
                logger.warning(
                    "Pre-compile job failed",
                    extra={"job": job_cls.name, "errors": result.errors, "project_id": getattr(project, "id", None)},
                )
            elif result.files_processed:
                logger.info(
                    "Pre-compile job completed",
                    extra={"job": job_cls.name, "files_processed": result.files_processed, "project_id": getattr(project, "id", None)},
                )
        except Exception as exc:
            logger.exception("Pre-compile job raised exception", extra={"job": job_cls.name, "project_id": getattr(project, "id", None)})
            results.append(PreCompileResult(job=job_cls.name, success=False, errors=[str(exc)]))
    return results
