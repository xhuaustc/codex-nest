"""Local stdio MCP entrypoint for the Codex Nest prototype."""
import base64
import json
import os
import subprocess
import sys
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
    "description": "Open the tree of projects registered on this Codex desktop host.",
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
    ("create_project", "Create a local Codex project",
     {**FIELDS, "requestId": ID}, ["name", "folders", "requestId"]),
    ("edit_project", "Update a local Codex project's name and working directories",
     {**FIELDS, "projectId": ID}, ["projectId", "name", "folders"]),
    ("open_project_folder", "Open a registered project directory in Finder",
     {"projectId": ID, "folder": ID}, ["projectId", "folder"]),
    ("open_project_chat", "Open a new Codex chat for this project in its primary directory",
     {"projectId": ID}, ["projectId"]),
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


def action_result(name, args):
    try:
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
            view = native_project(project, legacy_aliases(codex_home()))
            result = {"project": view}
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
                 "annotations": {"readOnlyHint": False, "destructiveHint": False},
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
