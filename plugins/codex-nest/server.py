"""Local stdio MCP entrypoint for the Codex Nest prototype."""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from project_api import AppServer, ProjectAPIError, project_fields

ROOT = Path(__file__).resolve().parent
URI = "ui://codex-nest/logical-project-tree"
MIME = "text/html;profile=mcp-app"
PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
ICON = ROOT / "assets" / "nest.svg"


def icon_for(theme, path):
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"src": f"data:image/svg+xml;base64,{encoded}", "mimeType": "image/svg+xml",
            "sizes": ["any"], "theme": theme}

TOOL = {
    "name": "logical_projects",
    "title": "Codex Nest",
    "description": "Open the tree of local and remote projects registered in Codex.",
    "icons": [icon_for("light", ICON), icon_for("dark", ICON.with_name("nest-dark.svg"))],
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
    "_meta": {
        "ui": {"resourceUri": URI},
        "openai/ui": {"entrypoints": [{"type": "global"}]},
    },
}
RESOURCE = {"uri": URI, "name": "Codex Nest", "mimeType": MIME}
ID = {"type": "string", "minLength": 1}
FIELDS = {"name": ID, "folders": {"type": "array", "minItems": 1, "items": ID}}
ACTION_TOOLS = [
    ("create_remote_project", "Register a project on a configured SSH host using Codex Desktop",
     {"hostId": ID, "name": ID, "path": ID}, ["hostId", "name", "path"]),
    ("create_project", "Create a local Codex project",
     {**FIELDS, "requestId": ID}, ["name", "folders", "requestId"]),
    ("edit_project", "Update a local Codex project's name and working directories",
     {**FIELDS, "projectId": ID}, ["projectId", "name", "folders"]),
    ("open_project_folder", "Open a registered project directory in Finder",
     {"projectId": ID, "folder": ID}, ["projectId", "folder"]),
    ("open_project_chat", "Open a new Codex chat for this project in its primary directory",
     {"projectId": ID}, ["projectId"]),
    ("archive_project_chats", "Archive the confirmed chats in a Codex project",
     {"projectId": ID, "hostId": ID, "chatIds": {"type": "array", "minItems": 1, "items": ID}},
     ["projectId", "chatIds"]),
]
API_ERRORS = (ProjectAPIError, OSError, ValueError, AttributeError, KeyError, TypeError)


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def read_local_state(home):
    return json.loads((home / ".codex-global-state.json").read_text(encoding="utf-8"))


def legacy_aliases(home):
    try:
        state = read_local_state(home)
        by_host = state.get("app-server-project-id-by-legacy-project-id-by-host", {})
        return {native: old for old, native in by_host.get(f"local:{home}", {}).items()}
    except API_ERRORS:
        return {}


def native_project(project, aliases):
    native_id = project["id"]
    folders = [root["path"] for root in project["roots"]]
    return {
        "id": aliases.get(native_id, native_id), "nativeId": native_id,
        "label": project["name"], "path": folders[0] if folders else "",
        "folders": folders, "chats": [],
    }


def mark_home_sync(project, registry):
    desktop = registry.get(project["id"])
    if isinstance(desktop, dict):
        project["homeSync"] = (desktop.get("name") == project["label"]
                               and desktop.get("rootPaths") == project["folders"])


def read_projects(home, *, include_chats=True):
    state = read_local_state(home)
    registry = state["local-projects"]
    assignments = state.get("thread-project-assignments", {})
    projects = {}
    for key, project in registry.items():
        roots = project["rootPaths"]
        label = project["name"]
        if not isinstance(roots, list) or not roots or not isinstance(label, str):
            raise ValueError("Malformed local project record")
        path = PurePosixPath(roots[0])
        if path.anchor != "/" or ".." in path.parts:
            raise ValueError("Malformed local project path")
        projects[key] = {"id": key, "path": str(path), "label": label, "folders": roots, "chats": []}

    if not include_chats:
        return list(projects.values())

    titles = read_thread_titles(home)
    for chat_id, assignment in assignments.items():
        if assignment.get("projectKind") == "local" and chat_id in titles:
            project = projects.get(assignment.get("projectId"))
            if project is not None:
                project["chats"].append({"id": chat_id, "title": titles[chat_id]})
    return list(projects.values())


def read_thread_titles(home):
    titles = {}
    try:
        with (home / "session_index.jsonl").open(encoding="utf-8") as index:
            for line in index:
                try:
                    row = json.loads(line)
                    chat_id, title = row["id"], row["thread_name"]
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(chat_id, str) and isinstance(title, str):
                    titles[chat_id] = title
    except FileNotFoundError:
        pass

    return titles


def read_remote_projects(home, *, include_chats):
    try:
        state = read_local_state(home)
    except API_ERRORS:
        return []
    host_names = {}
    for connection in state.get("codex-managed-remote-connections") or []:
        if not isinstance(connection, dict):
            continue
        host = connection.get("hostId")
        name = connection.get("displayName") or connection.get("alias")
        if isinstance(host, str) and isinstance(name, str) and name.strip():
            host_names[host] = name.strip()
    by_host = {}
    records = state.get("remote-projects")
    for entry in records if isinstance(records, list) else []:
        if not isinstance(entry, dict):
            continue
        project_id, host, path, label = (entry.get(key) for key in ("id", "hostId", "remotePath", "label"))
        if not all(isinstance(value, str) and value for value in (project_id, host, path, label)):
            continue
        by_host.setdefault(host, {})[project_id] = {
            "id": project_id, "hostId": host, "hostLabel": host_names.get(host), "label": label, "path": path,
            "folders": [path], "chats": [],
        }
    if include_chats and by_host:
        read_remote_chats(by_host, state)
    return [project for projects in by_host.values() for project in projects.values()]


def remote_ssh_hosts(state):
    connections = state.get("codex-managed-remote-connections") or []
    hosts = {}
    for connection in connections:
        if not isinstance(connection, dict) or connection.get("source") != "discovered":
            continue
        host, alias = connection.get("hostId"), connection.get("alias")
        if isinstance(host, str) and isinstance(alias, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", alias):
            hosts[host] = connection
    return hosts


def remote_app_server(host, state, deadline):
    connection = remote_ssh_hosts(state).get(host)
    if not connection:
        raise ProjectAPIError("remoteChatsUnavailable", "Remote chat history is unavailable.")
    command = ["/usr/bin/ssh", "-T", "-oBatchMode=yes", "-oConnectTimeout=4",
               "-oStrictHostKeyChecking=yes", "-oClearAllForwardings=yes", "-oForwardAgent=no",
               connection["alias"], """exec "$SHELL" -lc 'exec codex app-server --stdio'"""]
    return AppServer(command=command, timeout=5, deadline=deadline)


def read_remote_chats(by_host, state):
    assignments = state.get("thread-project-assignments") or {}
    deadline = time.monotonic() + 10
    for host, projects in by_host.items():
        try:
            if time.monotonic() >= deadline:
                raise ProjectAPIError("remoteChatsUnavailable", "Remote chat history is unavailable.")
            with remote_app_server(host, state, deadline) as api:
                threads = list(api.list_threads())
        except API_ERRORS:
            for project in projects.values():
                project["chatError"] = True
            continue
        for project in projects.values():
            project["hostReachable"] = True
        for thread in threads:
            chat_id = thread["id"]
            assignment = assignments.get(chat_id)
            if not isinstance(assignment, dict) or assignment.get("projectKind") != "remote" or assignment.get("hostId") != host:
                continue
            project_id = assignment.get("projectId")
            project = projects.get(project_id) if isinstance(project_id, str) else None
            if project is not None:
                title = thread.get("name") or thread.get("preview") or ""
                project["chats"].append({"id": chat_id, "title": title, "hostId": host})


def assign_chats(projects, threads, state, titles):
    by_id = {project["id"]: project for project in projects}
    by_native = {project["nativeId"]: project for project in projects}
    by_root = {}
    for project in projects:
        for folder in project["folders"]:
            by_root.setdefault(os.path.normpath(folder), set()).add(project["id"])
    legacy = state.get("thread-project-assignments", {})
    projectless = set(state.get("projectless-thread-ids", []))
    seen = set()

    for thread in threads:
        chat_id = thread["id"]
        if chat_id in seen:
            continue
        seen.add(chat_id)
        native_id = thread.get("projectId")
        assignment = legacy.get(chat_id)
        if native_id is not None:
            project = by_native.get(native_id)
        elif assignment:
            project = by_id.get(assignment.get("projectId")) if assignment.get("projectKind") == "local" else None
        elif chat_id in projectless:
            continue
        else:
            cwd = thread.get("cwd")
            matches = by_root.get(os.path.normpath(cwd), set()) if isinstance(cwd, str) else set()
            project = by_id[next(iter(matches))] if len(matches) == 1 else None
        if project is not None:
            title = thread.get("name") or titles.get(chat_id) or ""
            project["chats"].append({"id": chat_id, "title": title})


def project_payload(*, include_chats=True):
    home = codex_home()
    payload = local_project_payload(home, include_chats=include_chats)
    remote = read_remote_projects(home, include_chats=include_chats)
    if remote:
        payload.pop("error", None)
        payload.pop("errorKey", None)
        payload.setdefault("projects", []).extend(remote)
    try:
        state = read_local_state(home)
        hosts = remote_ssh_hosts(state)
        registry = state.get("local-projects", {})
        for project in payload.get("projects", []):
            if project.get("nativeId"):
                mark_home_sync(project, registry)
    except API_ERRORS:
        hosts = {}
    payload["remoteHosts"] = [
        {"id": host, "label": info.get("displayName") or info["alias"]}
        for host, info in hosts.items()
    ]
    return payload


def local_project_payload(home, *, include_chats):
    payload = {"source": "local_registry"}
    try:
        aliases = legacy_aliases(home)
        with AppServer() as api:
            payload["projects"] = [native_project(p, aliases) for p in api.list_projects()]
            if include_chats:
                try:
                    threads = list(api.list_threads())
                    try:
                        state = read_local_state(home)
                    except API_ERRORS:
                        state = {}
                    try:
                        titles = read_thread_titles(home)
                    except API_ERRORS:
                        titles = {}
                    assign_chats(payload["projects"], threads, state, titles)
                    payload["chatSource"] = "codex"
                except API_ERRORS:
                    payload["chatSource"] = "legacy"
        payload["canManage"] = True
    except API_ERRORS:
        payload["canManage"] = False
        if include_chats:
            payload["chatSource"] = "legacy"
        try:
            payload["projects"] = read_projects(home, include_chats=include_chats)
        except API_ERRORS:
            payload.update(error="Could not read local Codex projects.", errorKey="readProjectsFailed")
        return payload
    if not include_chats or payload.get("chatSource") == "codex":
        return payload
    try:
        catalog = {project["id"]: project["chats"] for project in read_projects(home)}
        for project in payload["projects"]:
            project["chats"] = catalog.get(project["id"], [])
    except API_ERRORS:
        pass
    return payload


def archive_project_chats(args):
    chat_ids = args["chatIds"]
    if not isinstance(chat_ids, list) or not chat_ids or not all(isinstance(item, str) and item for item in chat_ids):
        raise ValueError("Invalid chat selection")
    home = codex_home()
    deadline = time.monotonic() + 22
    host = args.get("hostId")
    if host:
        projects = read_remote_projects(home, include_chats=True)
    else:
        payload = local_project_payload(home, include_chats=True)
        if payload.get("chatSource") != "codex":
            raise ProjectAPIError("archiveProjectChatsUnavailable", "Could not verify the project's active chats.")
        projects = payload["projects"]
    project = next((item for item in projects if item["id"] == args["projectId"] and item.get("hostId") == host), None)
    if project is None or project.get("chatError"):
        raise ProjectAPIError("archiveProjectChatsUnavailable", "Could not verify the project's active chats.")
    requested = set(chat_ids)
    targets = [chat["id"] for chat in project["chats"] if chat["id"] in requested]
    archived = 0
    if targets:
        connection = remote_app_server(host, read_local_state(home), deadline) if host else AppServer(deadline=deadline)
        try:
            with connection as api:
                for chat_id in targets:
                    api.call("thread/archive", {"threadId": chat_id})
                    archived += 1
        except API_ERRORS as error:
            raise ProjectAPIError("archiveProjectChatsIncomplete", "Could not confirm the full archive operation.",
                                  count=archived) from error
    return {"archivedCount": archived}


def action_result(name, args):
    try:
        if name == "create_remote_project":
            result = create_remote_project(args)
        elif name == "archive_project_chats":
            result = archive_project_chats(args)
        else:
            result = manage_project(name, args)
    except (ProjectAPIError, OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError) as error:
        if isinstance(error, ProjectAPIError):
            payload = {"error": str(error), "errorKey": error.key, "errorParams": error.params}
        else:
            payload = {"error": "Could not complete the operation. Check Codex and folder permissions.",
                       "errorKey": "actionFailed"}
        return {"isError": True, "content": [{"type": "text", "text": payload["error"]}],
                "structuredContent": payload}
    return {"content": [{"type": "text", "text": "Codex project action completed."}],
            "structuredContent": result}


def find_remote_project(home, host, path):
    return next((project for project in read_remote_projects(home, include_chats=False)
                 if project["hostId"] == host and PurePosixPath(project["path"]) == path), None)


def create_remote_project(args):
    name = args["name"].strip()
    path = PurePosixPath(args["path"].strip())
    if not name:
        raise ProjectAPIError("projectNameRequired", "Enter a project name.")
    if path.anchor != "/" or ".." in path.parts or "\x00" in str(path):
        raise ProjectAPIError("remotePathRequired", "Enter an absolute directory path on the remote host.")
    home = codex_home()
    host = args["hostId"]
    state = read_local_state(home)
    connection = remote_ssh_hosts(state).get(host)
    if not connection:
        raise ProjectAPIError("remoteHostUnavailable", "This SSH host is not available in Codex.")
    existing = find_remote_project(home, host, path)
    if existing is not None:
        return {"project": existing, "alreadyRegistered": True}

    config = home / "codex-app" / "config.json"
    if os.path.lexists(config):
        raise ProjectAPIError("desktopConfigBusy", "Codex desktop setup config already exists.")
    try:
        with remote_app_server(host, state, time.monotonic() + 8) as api:
            info = api.call("fs/getMetadata", {"path": str(path)})
    except API_ERRORS as error:
        raise ProjectAPIError("remotePathUnavailable", "Could not verify the remote directory.") from error
    if not info.get("isDirectory"):
        raise ProjectAPIError("remotePathUnavailable", "Could not verify the remote directory.")
    existing = find_remote_project(home, host, path)
    if existing is not None:
        return {"project": existing, "alreadyRegistered": True}

    setup = {"version": 1, "remoteConnections": [{"sshAlias": connection["alias"],
                                                  "projects": [{"remotePath": str(path), "label": name}]}]}
    retry_limit = state.get("remote-connection-max-retry-attempts")
    if type(retry_limit) is int and retry_limit >= 0:
        setup["remoteConnectionMaxRetryAttempts"] = retry_limit
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=config.parent, prefix=".nest-project-", encoding="utf-8") as staged:
        json.dump(setup, staged)
        staged.flush()
        try:
            os.link(staged.name, config)
        except FileExistsError as error:
            raise ProjectAPIError("desktopConfigBusy", "Codex desktop setup config already exists.") from error
        try:
            subprocess.run(["/usr/bin/open", "-g", "-b", "com.openai.codex", "codex://codex-app/apply-config"],
                           check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                project = find_remote_project(home, host, path)
                if project is not None:
                    return {"project": project}
                time.sleep(0.25)
        finally:
            if config.is_file() and not config.is_symlink() and os.path.samefile(staged.name, config):
                config.unlink(missing_ok=True)
    raise ProjectAPIError("remoteRegistrationUnconfirmed", "Codex did not confirm registration. Refresh before retrying.")


def manage_project(name, args):
    fields = project_fields(args["name"], args["folders"]) if name in {"create_project", "edit_project"} else None
    with AppServer() as api:
        if name == "create_project":
            project = api.call("project/create", {**fields, "idempotencyKey": args["requestId"]})["project"]
        elif name == "edit_project":
            project = api.call("project/update", {**fields, "projectId": args["projectId"]})["project"]
        else:
            project = api.call("project/read", {"projectId": args["projectId"]})["project"]
    if name == "open_project_folder":
        folder = args["folder"]
        if folder not in [root["path"] for root in project["roots"]] or not Path(folder).is_dir():
            raise ProjectAPIError("folderUnavailable", "The working directory has changed or is unavailable. Refresh and try again.")
        subprocess.run(["/usr/bin/open", "-a", "Finder", folder], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = {"opened": True}
    elif name == "open_project_chat":
        view = native_project(project, legacy_aliases(codex_home()))
        primary = Path(view["path"])
        if not primary.is_absolute() or not primary.is_dir():
            raise ProjectAPIError("primaryUnavailable", "The primary project directory is unavailable.")
        # Use the identity known to Desktop, which may differ from the app-server ID.
        query = urlencode({"projectId": view["id"], "path": str(primary)}, quote_via=quote)
        subprocess.run(["/usr/bin/open", "-a", "ChatGPT", "codex://threads/new?" + query],
                       check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = {"opened": True}
    else:
        home = codex_home()
        view = native_project(project, legacy_aliases(home))
        if name == "edit_project":
            try:
                mark_home_sync(view, read_local_state(home).get("local-projects", {}))
            except API_ERRORS:
                pass
        result = {"project": view}
    return result


def render_resource():
    template = (ROOT / "projects.html").read_text(encoding="utf-8")
    mark = ICON.read_text(encoding="utf-8").replace('stroke="#252525"', 'stroke="currentColor"')
    template = template.replace('<span class="nest-mark" aria-hidden="true"></span>',
                                f'<span class="nest-mark" aria-hidden="true">{mark}</span>', 1)
    payload = project_payload(include_chats=False)
    payload["initial"] = True
    # Prevent paths or project names from closing the inert JSON script element.
    encoded = json.dumps(payload).replace("<", "\\u003c")
    return template.replace('id="initial-projects">{}</script>', f'id="initial-projects">{encoded}</script>', 1)


class Server:
    def dispatch(self, method, params):
        if method == "initialize":
            version = params["protocolVersion"]
            return {
                "protocolVersion": version if version in PROTOCOLS else "2025-11-25",
                "serverInfo": {"name": "codex-nest", "version": "0.4.14", "icons": TOOL["icons"]},
                "capabilities": {"tools": {}, "resources": {}},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [TOOL, *[
                {"name": name, "description": description,
                 "inputSchema": {"type": "object", "properties": props, "required": required,
                                 "additionalProperties": False},
                 "annotations": {"readOnlyHint": False, "destructiveHint": name == "archive_project_chats"},
                 "_meta": {"ui": {"resourceUri": URI, "visibility": ["app"]}}}
                for name, description, props, required in ACTION_TOOLS
            ]]}
        if method == "resources/list":
            return {"resources": [RESOURCE]}
        if method == "resources/read" and params.get("uri") == URI:
            return {"contents": [{**RESOURCE, "text": render_resource()}]}
        if method == "tools/call" and params.get("name") == TOOL["name"]:
            if params.get("arguments"):
                raise ValueError("Unexpected tool arguments")
            payload = project_payload()
            return {
                "isError": "error" in payload,
                "content": [{"type": "text", "text": payload.get("error", "Opened Codex Nest.")}],
                "structuredContent": payload,
            }
        if method == "tools/call" and params.get("name") in {tool[0] for tool in ACTION_TOOLS}:
            return action_result(params["name"], params.get("arguments", {}))
        raise LookupError(method)


def main():
    server = Server()
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}), flush=True)
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            reply["result"] = server.dispatch(request["method"], request.get("params", {}))
        except (ValueError, TypeError, KeyError, AttributeError):
            reply["error"] = {"code": -32602, "message": "Invalid parameters"}
        except LookupError:
            reply["error"] = {"code": -32601, "message": "Method not found"}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
