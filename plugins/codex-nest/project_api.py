"""Client for the desktop Codex app-server's local project API."""

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path

DESKTOP_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


class ProjectAPIError(Exception):
    def __init__(self, key, message, **params):
        super().__init__(message)
        self.key = key
        self.params = params


class AppServer:
    def __init__(self, command=None, timeout=8):
        binary = str(DESKTOP_CODEX) if DESKTOP_CODEX.is_file() else shutil.which("codex")
        if command is None and binary is None:
            raise ProjectAPIError("missingAppServer", "Codex app-server could not be found.")
        self.command = command if command is not None else [binary, "app-server", "--stdio"]
        self.timeout = timeout
        self.next_id = 0
        self.buffer = b""

    def __enter__(self):
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.selector = selectors.DefaultSelector()
        try:
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.call("initialize", {
                "clientInfo": {"name": "codex_nest", "version": "0.4.14"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"method": "initialized"})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ProjectAPIError("apiUnavailable", "The Codex project API did not respond.")
                self.buffer += chunk
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                code = message["error"].get("code")
                if code == -32601:
                    raise ProjectAPIError("apiUnsupported", "This Codex version does not support the project API.")
                raise ProjectAPIError("apiFailed", f"The Codex project operation failed ({code}).", code=code)
            return message["result"]
        raise ProjectAPIError("apiTimeout", "The Codex operation timed out. Refresh to check the result.")

    def list_projects(self):
        yield from self.list_pages("project/list")

    def list_threads(self):
        yield from self.list_pages("thread/list", archived=False, useStateDbOnly=True, modelProviders=[])

    def list_pages(self, method, **filters):
        cursor = None
        seen = set()
        while True:
            params = {"limit": 100, **filters}
            if cursor:
                params["cursor"] = cursor
            result = self.call(method, params)
            yield from result["data"]
            cursor = result.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise ProjectAPIError("repeatedCursor", "Codex returned a repeated pagination cursor.")
            seen.add(cursor)


def project_fields(name, folders):
    if not isinstance(name, str) or not name.strip():
        raise ProjectAPIError("projectNameRequired", "Enter a project name.")
    if not isinstance(folders, list) or not folders:
        raise ProjectAPIError("folderRequired", "Enter at least one working directory.")
    roots = []
    seen = set()
    for folder in folders:
        if not isinstance(folder, str) or not folder.strip():
            raise ProjectAPIError("invalidFolder", "Enter a valid working directory.")
        path = Path(folder.strip()).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ProjectAPIError("folderMustExist", "The working directory must be an existing local directory.")
        normalized = os.path.normpath(path)
        if normalized not in seen:
            seen.add(normalized)
            roots.append({"path": normalized})
    return {"name": name.strip(), "roots": roots}
