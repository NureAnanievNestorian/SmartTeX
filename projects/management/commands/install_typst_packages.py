"""Management command: pre-download Typst packages to TYPST_PACKAGES_DIR.

Packages listed in TYPST_PREINSTALLED_PACKAGES (settings or --packages flag)
are compiled into XDG_DATA_HOME=TYPST_PACKAGES_DIR so subsequent compilations
(native or Docker) can find them without hitting the network.

Package format: preview/<name>:<version>  (e.g. preview/cetz:0.3.0)

Usage:
    python manage.py install_typst_packages
    python manage.py install_typst_packages --packages preview/cetz:0.3.0,preview/tablex:0.0.9
    python manage.py install_typst_packages --packages-dir /srv/typst-packages
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Pre-download Typst packages into TYPST_PACKAGES_DIR."

    def add_arguments(self, parser):
        parser.add_argument(
            "--packages",
            default="",
            help=(
                "Comma-separated package specs to install, e.g. preview/cetz:0.3.0. "
                "Defaults to TYPST_PREINSTALLED_PACKAGES from settings."
            ),
        )
        parser.add_argument(
            "--packages-dir",
            default="",
            help=(
                "Directory to use as XDG_DATA_HOME for Typst package storage. "
                "Defaults to TYPST_PACKAGES_DIR from settings."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be installed without doing it.",
        )

    def handle(self, *args, **options):
        packages_dir = options["packages_dir"].strip() or str(
            getattr(settings, "TYPST_PACKAGES_DIR", "")
        ).strip()
        if not packages_dir:
            raise CommandError(
                "No packages directory configured. "
                "Set TYPST_PACKAGES_DIR in settings or pass --packages-dir."
            )

        raw_packages = options["packages"].strip()
        if raw_packages:
            packages = [p.strip() for p in raw_packages.split(",") if p.strip()]
        else:
            packages = list(getattr(settings, "TYPST_PREINSTALLED_PACKAGES", []))

        if not packages:
            self.stdout.write("No packages to install.")
            return

        typst_bin = str(getattr(settings, "TYPST_BINARY", "typst")).strip() or "typst"

        Path(packages_dir).mkdir(parents=True, exist_ok=True)

        self.stdout.write(
            f"Installing {len(packages)} package(s) into {packages_dir}"
            + (" [DRY RUN]" if options["dry_run"] else "")
        )

        ok = failed = 0
        for spec in packages:
            self.stdout.write(f"  @{spec} ...", ending="")
            self.stdout.flush()

            if options["dry_run"]:
                self.stdout.write(" skipped (dry-run)")
                continue

            success, err = self._install_package(typst_bin, packages_dir, spec)
            if success:
                self.stdout.write(self.style.SUCCESS(" ok"))
                ok += 1
            else:
                self.stdout.write(self.style.ERROR(f" FAILED\n    {err}"))
                failed += 1

        if not options["dry_run"]:
            self.stdout.write(f"\nDone. ok={ok} failed={failed}")
            if failed:
                sys.exit(1)

    @staticmethod
    def _install_package(typst_bin: str, packages_dir: str, spec: str) -> tuple[bool, str]:
        """Compile a minimal document that imports `spec` to trigger the download."""
        src = textwrap.dedent(f"""\
            #import "@{spec}": *
            #set page(width: 1pt, height: 1pt, margin: 0pt)
        """)
        env = os.environ.copy()
        env["XDG_DATA_HOME"] = packages_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "pkg_check.typ"
            out_file = Path(tmpdir) / "pkg_check.pdf"
            src_file.write_text(src, encoding="utf-8")

            try:
                result = subprocess.run(
                    [typst_bin, "compile", "--root", tmpdir, str(src_file), str(out_file)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                if result.returncode == 0:
                    return True, ""
                stderr = (result.stderr or result.stdout or "").strip()
                return False, stderr[:300]
            except FileNotFoundError:
                return False, f"typst binary not found: {typst_bin!r}"
            except subprocess.TimeoutExpired:
                return False, "timed out after 120s"
            except Exception as exc:
                return False, str(exc)
