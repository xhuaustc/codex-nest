# Store project hierarchy separately from working directories

Directory ancestry can differ from how a user organizes their work. Codex Nest stores user-chosen parents using stable Codex project IDs and plugin group IDs. Moving a node changes the plugin's layout without moving files or changing Codex project registration.

Only groups can contain children. When loading an older layout, a node previously under a project appears under the nearest ancestor group, or at the top level if there is none. Edits save the new layout separately from the old record.
