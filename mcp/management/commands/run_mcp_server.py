import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request
from urllib.parse import urlencode

from django.core.management.base import BaseCommand, CommandError


@dataclass
class APIClient:
    base_url: str
    token: str

    def _call(self, method: str, path: str, data: dict | None = None) -> Any:
        url = f"{self.base_url.rstrip('/')}{path}"
        body = None
        headers = {"Authorization": f"Token {self.token}"}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, method=method, headers=headers, data=body)
        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="ignore")
            raise CommandError(f"HTTP {exc.code}: {payload}") from exc

    def list_projects(self):
        return self._call("GET", "/api/projects/")

    def get_project(self, project_id: int):
        return self._call("GET", f"/api/projects/{project_id}/")

    def get_project_file(self, project_id: int):
        return self._call("GET", f"/api/projects/{project_id}/file/")

    def update_project_file(self, project_id: int, content: str):
        return self._call("PUT", f"/api/projects/{project_id}/file/", {"content": content})

    def compile_project(self, project_id: int):
        return self._call("POST", f"/api/projects/{project_id}/compile/")

    def get_compile_log(self, project_id: int):
        return self._call("GET", f"/api/projects/{project_id}/compile/")

    def list_templates(self):
        return self._call("GET", "/api/templates/")

    def search_project_content(
        self,
        project_id: int,
        query: str,
        is_regex: bool = False,
        ignore_case: bool = True,
        max_results: int = 200,
        include_main: bool = True,
        include_assets: bool = True,
    ):
        params = urlencode(
            {
                "query": query,
                "is_regex": str(bool(is_regex)).lower(),
                "ignore_case": str(bool(ignore_case)).lower(),
                "max_results": int(max_results),
                "include_main": str(bool(include_main)).lower(),
                "include_assets": str(bool(include_assets)).lower(),
            }
        )
        return self._call("GET", f"/api/projects/{project_id}/search/?{params}")

    def read_project_window(
        self,
        project_id: int,
        file_name: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
    ):
        resolved_file_name = file_name or str(self.get_project(project_id).get("main_file_name") or "main.tex")
        query: dict[str, Any] = {"file_name": resolved_file_name}
        if start_line is not None:
            query["start_line"] = int(start_line)
        if end_line is not None:
            query["end_line"] = int(end_line)
        if start_char is not None:
            query["start_char"] = int(start_char)
        if end_char is not None:
            query["end_char"] = int(end_char)
        return self._call("GET", f"/api/projects/{project_id}/read-window/?{urlencode(query)}")


class Command(BaseCommand):
    help = "Run lightweight MCP bridge over stdio (JSON lines)."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", required=True, help="Django base URL, e.g. http://127.0.0.1:8000")
        parser.add_argument("--token", required=True, help="MCP API token from /api/mcp/token/")

    def handle(self, *args, **options):
        client = APIClient(base_url=options["base_url"], token=options["token"])

        self.stdout.write(
            json.dumps(
                {
                    "type": "hello",
                    "protocol": "smarttex-mcp-bridge-v1",
                    "tools": [
                        "list_projects",
                        "get_project",
                        "get_project_file",
                        "update_project_file",
                        "compile_project",
                        "get_compile_log",
                        "list_templates",
                        "search_project_content",
                        "read_project_window",
                    ],
                    "input_format": {"tool": "name", "args": {}},
                },
                ensure_ascii=False,
            )
        )

        while True:
            try:
                line = input().strip()
            except EOFError:
                break

            if not line:
                continue

            try:
                payload = json.loads(line)
                tool = payload.get("tool")
                args = payload.get("args", {})

                if tool == "list_projects":
                    result = client.list_projects()
                elif tool == "get_project_file":
                    result = client.get_project_file(int(args["project_id"]))
                elif tool == "get_project":
                    result = client.get_project(int(args["project_id"]))
                elif tool == "update_project_file":
                    result = client.update_project_file(int(args["project_id"]), str(args.get("content", "")))
                elif tool == "compile_project":
                    result = client.compile_project(int(args["project_id"]))
                elif tool == "get_compile_log":
                    result = client.get_compile_log(int(args["project_id"]))
                elif tool == "list_templates":
                    result = client.list_templates()
                elif tool == "search_project_content":
                    result = client.search_project_content(
                        int(args["project_id"]),
                        str(args.get("query", "")),
                        bool(args.get("is_regex", False)),
                        bool(args.get("ignore_case", True)),
                        int(args.get("max_results", 200)),
                        bool(args.get("include_main", True)),
                        bool(args.get("include_assets", True)),
                    )
                elif tool == "read_project_window":
                    result = client.read_project_window(
                        int(args["project_id"]),
                        str(args["file_name"]) if args.get("file_name") is not None else None,
                        int(args["start_line"]) if "start_line" in args and args["start_line"] is not None else None,
                        int(args["end_line"]) if "end_line" in args and args["end_line"] is not None else None,
                        int(args["start_char"]) if "start_char" in args and args["start_char"] is not None else None,
                        int(args["end_char"]) if "end_char" in args and args["end_char"] is not None else None,
                    )
                else:
                    raise CommandError(f"Unknown tool: {tool}")

                self.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
            except Exception as exc:  # pragma: no cover
                self.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
