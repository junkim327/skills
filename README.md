# Jira Ticket Workflow

[![skills.sh](https://skills.sh/b/junkim327/skills)](https://skills.sh/junkim327/skills)

A Jira-backed workflow for maintaining repository-based AI agents. It separates running an agent from fixing or creating one, inspects the real codebase before planning, requires human approval before Jira writes, isolates each implementation in a task worktree, and carries the approved change through review and handoff.

## Install

```bash
npx skills@latest add junkim327/skills --skill jira-ticket-workflow
```

Install for a specific coding agent:

```bash
npx skills@latest add junkim327/skills --skill jira-ticket-workflow --agent claude-code
npx skills@latest add junkim327/skills --skill jira-ticket-workflow --agent codex
npx skills@latest add junkim327/skills --skill jira-ticket-workflow --agent cursor
```

Test a local checkout with:

```bash
npx skills@latest add . --skill jira-ticket-workflow
```

## Quick setup

From the target agent repository, ask the skill to guide setup.

In Claude Code:

```text
/jira-ticket-workflow setup this repository
```

In Codex:

```text
$jira-ticket-workflow setup this repository
```

In Cursor, invoke `jira-ticket-workflow` from the slash-command menu and ask it to set up the repository.

The skill discovers accessible Jira projects, issue types, and statuses through read-only calls; inspects repository Git conventions; verifies required Jira–GitHub status synchronization; previews `.jira-ticket-workflow.json`; asks before writing it; and finishes with a read-only readiness report.

## Jira connection

The skill prefers a Jira MCP connector exposed by Claude Code, Cursor, or Codex. It uses the bundled Jira Cloud REST adapter only when no capable Jira MCP is available or the user explicitly selects REST. An MCP authentication or permission failure is never bypassed by silently switching to REST.

The REST fallback uses:

```bash
export JIRA_EMAIL="you@example.com"
export JIRA_API_TOKEN="..."
```

Keep credentials and real Jira account IDs out of source control.

To check an existing REST fallback setup in Claude Code:

```text
/jira-ticket-workflow run the readiness check for this repository
```

In Codex:

```text
$jira-ticket-workflow run the readiness check for this repository
```

Or call the bundled helper directly:

```bash
python3 "<skill-dir>/scripts/jira_workflow.py" \
  --config .jira-ticket-workflow.json check --repo . --jira-connection rest
```

When Jira was verified through MCP, run the local configuration and Git/GitHub
portion with `--jira-connection mcp --verified-external-check jira_mcp`. Never
pass the external-check option before the host has collected the corresponding
evidence.

The default `jira_status_sync: automated` mode requires GitHub for Atlassian,
target repository access, Jira-key linkage, enabled PR-created and PR-merged
Automation rules, required workflow transitions, and Automation actor
permissions. Missing, misconfigured, or unverified status-sync configuration
blocks readiness.
Use explicit `manual` mode when a person will own Jira review and done
transitions. A missing Jira configuration, Jira permission, or Git repository
also remains a blocker. GitHub authentication and repository access are blockers
when `pull_request.provider` is `github`.

For a private team repository, one maintainer can commit a configuration that contains no credentials or sensitive account IDs; teammates then need either an authorized Jira MCP connection or their REST fallback environment variables and a passing readiness result. Keep the configuration ignored in public repositories.

The bundled fallback adapter targets Jira Cloud REST API v3 with email and API-token authentication. The same discovery, approval, implementation, and handoff rules apply when Jira MCP is selected.

Requirements for the bundled adapter: Python 3.10 or later and a Jira Cloud account permitted to search, create, comment, assign, and transition issues as configured.

All helper write commands preview by default. After the user approves the exact preview, add `--write` to `create`, `comment`, or `transition`.

## Workflow

```text
classify request
  → inspect prompt versus repository reality
  → resolve consequential unknowns
  → pass read-only setup checks
  → search Jira for duplicates
  → show ticket draft and obtain approval
  → create/select ticket
  → create or reuse a task-dedicated worktree and move to In Progress
  → implement and validate
  → explain the change in plain language
  → open PR; Jira Automation moves the ticket to In Review
  → approved merge; Jira Automation moves the ticket to Done
```

The skill keeps discovery terminology internal. Jira contains the stable behavior contract; the pull request contains implementation detail and evidence.

## Repository structure

```text
skills/jira-ticket-workflow/
├── SKILL.md
├── config.example.json
├── agents/openai.yaml
├── references/
│   ├── github-integration.md
│   └── ...
└── scripts/jira_workflow.py
```

This follows the public `skills/<name>/SKILL.md` publishing pattern used by repositories such as [`anthropics/skills`](https://github.com/anthropics/skills).

Confirm that you are authorized to publish the workflow and its derived material before creating a public remote. The included license is MIT; change it before publishing if a different license is required.

## Development

```bash
python3 -m unittest discover -s tests -v
npx skills@latest add . --list
```

## License

MIT

Jira is a trademark of Atlassian. This independent project is not affiliated with or endorsed by Atlassian.
