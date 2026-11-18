# Show the project tree in a plugin view

Codex Nest will show its project tree in a dedicated plugin view, with an entry in the Codex desktop sidebar as an acceptance criterion. The native sidebar tools expose flat project grouping and ordering, while the documented plugin extension provides an entry to a separate app view. Verify that entry in Codex mode with an installed prototype before building the full management workflow.

On ChatGPT 26.1103.60933 (build 25894), the user could open the project tree through its direct link, but the sidebar entry did not appear. A separate local Codex app-server session discovered `show_projects` with its `global` entrypoint and MCP App resource. Desktop sidebar rendering remains an open acceptance item.
