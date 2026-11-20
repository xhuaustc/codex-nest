## Git delivery

When asked to commit changes, push the same commit to `origin` and `gitlab` by default. Use `main` for both unless the user specifies another branch.

Keep the private `gitlab` URL in the local Git configuration. If the remote is missing, report it as blocked. After pushing, use `git ls-remote` to verify each target branch points to the intended commit and report each result separately.

## Agent skills

### Issue tracker

Before reading or updating issues or specs in GitHub Issues for `xhuaustc/codex-nest`, read `docs/agents/issue-tracker.md`.

### Triage labels

When triaging issues, read the five default labels and their meanings in `docs/agents/triage-labels.md`.

### Domain docs

Before exploring domain code or proposing architectural changes, read the single-context layout and rules in `docs/agents/domain.md`.
