---
name: jira-ticket-workflow
description: Set up, verify, and run a Jira-backed workflow for developing repository-based AI agents. Use when an existing agent's code, instructions, routing, queries, thresholds, tools, or output must be fixed; when a wrong result or repeated failure may require a repository change; when a new agent is requested and no existing agent clearly owns the capability; or when the repository needs Jira workflow setup or a readiness check. Inspect the request against the actual codebase, surface consequential unknowns, obtain human approval, then implement and hand off through Jira and a pull request. Do not use merely to run an agent or for transient external-service, credential, permission, or data-availability failures unless repository behavior must change.
---

# Jira Ticket Workflow

Treat the user's prompt, plan, or specification as the **map** and the actual codebase, integrations, and operating constraints as the **territory**. Use the difference to find decisions the request did not specify.

Resolve `<skill-dir>` to the directory containing this `SKILL.md`; never assume a particular installation path.

Read these resources when needed:

- Before the first Jira write or when setup is unclear, read `references/configuration.md`.
- Before setup or readiness verification when `pull_request.jira_status_sync` is
  `automated`, read `references/github-integration.md`.
- Before drafting a Jira ticket or pull request, read `references/content-format.md`.

## Jira connection strategy

Prefer a Jira MCP connector exposed by the current host. Use the bundled Python helper as the Jira Cloud REST fallback only when no Jira MCP is available, the MCP lacks a required capability, or the user explicitly selects REST.

- Keep one Jira connection for the whole workflow and state whether it is `MCP` or `REST fallback`.
- Do not bypass an MCP authentication, permission, policy, or data error by silently switching to REST.
- Map MCP operations by capability: identity, project discovery, issue metadata, search, create, comment, status, and transition.
- Apply the same project scope, preview, approval, and sensitive-data rules to both connections.
- For MCP, verify Jira readiness with read-only MCP calls, then run the helper's local Git/GitHub checks with `check --jira-connection mcp`. Treat its `external_checks_required: ["jira_mcp"]` as satisfied only after the MCP checks pass.
- For REST fallback, use the helper's `check --jira-connection rest`; Jira credentials stay in environment variables.

## Non-negotiable gates

- Inspect and diagnose before editing or creating Jira work.
- Resolve every decision that could materially change architecture, scope, security, permissions, side effects, ownership, or acceptance criteria.
- Search open and resolved Jira work before creating a ticket.
- Pass the read-only readiness checks before Jira writes or repository edits. The only setup exception is writing the explicitly approved local workflow configuration required by `check`.
- Preview the exact operation and obtain explicit approval before every
  agent-initiated Jira write. Jira Automation rules approved during setup may
  perform their configured PR-driven transitions without per-event approval.
- Create or select a Jira ticket before implementation.
- Move the ticket to the configured in-progress status after creating the local branch or worktree and before the first edit.
- When Jira status sync is automated, verify the GitHub for Atlassian connection,
  repository access, Jira-key linkage, enabled automation rules, workflow
  transitions, Automation actor, and GitHub merge controls before implementation.
- Keep credentials, personal data, sensitive production data, raw logs, and temporary investigation files out of Jira, commits, and pull requests.
- Never approve or merge the pull request for the user.

If `.jira-ticket-workflow.json` is absent, continue through read-only discovery and guided setup. Write only the explicitly approved local workflow configuration, then pass readiness before Jira writes or implementation edits. Never invent Jira configuration.

## Setup and readiness

When the user asks to set up the workflow, or configuration is missing:

1. Read `references/configuration.md` and select `MCP` or `REST fallback` using the connection strategy above.
2. Show the configuration template without writing:

   ```text
   python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json setup
   ```

3. Discover the Jira site, accessible projects, issue types, and statuses without writes:
   - For MCP, use the host's Jira MCP tools.
   - For REST fallback, check only whether `JIRA_EMAIL` and `JIRA_API_TOKEN` are present, then run:

     ```text
     python3 "<skill-dir>/scripts/jira_workflow.py" setup --base-url "https://<site>.atlassian.net"
     python3 "<skill-dir>/scripts/jira_workflow.py" setup --base-url "https://<site>.atlassian.net" --project-key <PROJECT>
     ```

   Never ask the user to paste credentials into the conversation.
4. Inspect repository branch, commit, and pull-request conventions. Ask one setup question at a time. Use the host's structured question tool when available; otherwise ask directly in chat and wait.
5. Build the proposed configuration at a temporary path outside the repository. Preview it:

   ```text
   python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json setup --input <temp-config>
   ```

6. Show the sanitized preview and obtain explicit approval before writing. Then write it atomically:

   ```text
   python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json setup --input <temp-config> --write
   ```

7. Run the read-only readiness checks and resolve every blocker:
   - For MCP, verify Jira identity, project access, search, issue types, statuses, and required permissions with MCP, then run:

     ```text
     python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json check --repo . --jira-connection mcp
     ```

   - For REST fallback, run:

     ```text
     python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json check --repo . --jira-connection rest
     ```

When `pull_request.jira_status_sync` is `automated`, complete the read-only
preflight in `references/github-integration.md`. Treat `missing`,
`misconfigured`, and `unverified` results as blockers. Only after the host has
collected evidence for every required item may it rerun `check` with
the four automated-sync `--verified-external-check` values listed in
`references/github-integration.md`. For MCP, also pass
`--verified-external-check jira_mcp` only after its Jira checks pass. These
flags are one-run attestations, not substitutes for inspection.

Treat missing Jira configuration, project access, issue type, status, or Git as blockers. REST fallback also requires Jira credentials. When `pull_request.provider` is `github`, require GitHub authentication and repository access. `check` must never create or change Jira work, automation rules, branches, commits, pushes, or pull requests.

## 1. Classify the request

Classify it before choosing an agent:

- `run`: execute an existing agent without repository development. Do not create a development ticket.
- `fix`: correct a reproducible failure, documented-rule violation, unsafe or wrong output, routing defect, or missing error handling in an existing agent.
- `new`: add a capability that no existing agent should own.
- `external`: report a transient credential, API, permission, or source-data failure without a development ticket unless repository behavior should improve.
- `unclear`: continue read-only investigation or ask one focused question.

For `new`, inspect the root router and neighboring agents first. Extend an existing owner when the domain, inputs, tools, and output contract substantially overlap.

## 2. Select discovery depth

- Use `light` for a narrow, reproducible, low-risk fix with clear expected behavior, clear tests, and no new external side effect.
- Use `full` for a new agent, unclear ownership, routing or architecture change, external write, security or asset risk, broad refactor, weak tests, or a consequential unstated preference.

Keep `light` proportional. Inspect the target instructions, failing path, relevant tests, and nearest shared consumer or reference unless evidence points wider.

## 3. Compare map and territory

Extract from the map:

- intended outcome;
- stated constraints and preferences;
- expected behavior and acceptance criteria;
- explicit assumptions, references, and non-goals.

Inspect the relevant territory without editing:

- repository and agent-local instructions;
- implementation, tests, fixtures, and recent run artifacts;
- neighboring agents and routing boundaries;
- shared clients and external-system contracts;
- history, design notes, known gotchas, and difficult modules;
- actual Jira workflow transitions when lifecycle behavior matters.

Run a blind-spot pass. For `full`, reason explicitly about:

- **Known knowns:** confirmed desired behavior and facts.
- **Known unknowns:** information known to be missing.
- **Unknown knowns:** assumptions or team knowledge that felt obvious but was never written down.
- **Unknown unknowns:** plausible blind spots found by inspecting the territory. State what was inspected and what risk remains; never claim completeness.

Keep this framework in the conversation or temporary notes. Do not copy its headings into Jira or the pull request.

## 4. Resolve consequential unknowns

Use the least expensive method that makes the decision concrete:

1. Inspect an authoritative reference or the nearest analogous agent.
2. Offer deliberately different prototypes when preferences are hard to verbalize.
3. Ask one question at a time, highest architectural impact first.

Use the host's structured question tool when available (`AskUserQuestion` in Claude Code, `AskQuestion` in Cursor, or `request_user_input` in supported Codex modes). If the current host or mode exposes no structured question tool, ask one concise question in plain chat and wait. Ask only when the answer can materially change the solution. Record lower-risk uncertainty as an explicit assumption.

When a material decision remains, return `Discovery blocked` with concise findings, meaningful options and trade-offs, and the first blocking question. Stop before the final Jira draft.

## 5. Search Jira for duplicates

Pass the connection-specific readiness checks first. For REST fallback, run:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json check --repo . --jira-connection rest
```

For MCP, complete the MCP Jira checks and run `check --jira-connection mcp` for Git/GitHub. Then search by agent and symptom or capability using the selected connection. The REST fallback command is:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json search --agent <agent-slug> --terms "<short symptom>"
```

Classify candidates:

- High-confidence open duplicate: propose adding evidence to it.
- Matching resolved issue: propose a regression ticket linked to it.
- Related but distinct issue: propose a new ticket referencing it.
- No meaningful match: propose a new ticket.

## 6. Present the approval packet

For `light`, show:

1. diagnosis and reproduction;
2. consequential assumptions and non-goals;
3. duplicate-search result;
4. proposed summary, scope, acceptance criteria, validation, impact, and rollback;
5. one approval question.

For `full`, also show territory inspected, the unknown matrix, resolved decisions, remaining non-blocking questions, references, ownership rationale, and recommended action (`comment existing` or `create new`).

Use the host's structured question tool when available; otherwise ask directly in chat. A plain-chat answer counts as approval only when the user clearly confirms the exact proposed Jira write. A run request or development request authorizes investigation, not a Jira write.

## 7. Create or update Jira

Build the ticket input described in `references/content-format.md` in a temporary path outside the repository. The helper previews by default without contacting Jira:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json create --input <temp-dir>/jira-ticket-workflow-ticket.json
```

Inspect the summary, fields, description, labels, priority, assignee behavior, and configured CC behavior. Obtain approval for the exact preview. Then create through the selected connection:

- For MCP, map the approved preview to the MCP create tool.
- For REST fallback, run:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json create --input <temp-dir>/jira-ticket-workflow-ticket.json --write
```

For a duplicate, preview the sanitized evidence comment, obtain approval, then write through MCP or add `--write` for REST fallback:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json comment <ISSUE-KEY> --input <temp-dir>/jira-ticket-workflow-comment.json
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json comment <ISSUE-KEY> --input <temp-dir>/jira-ticket-workflow-comment.json --write
```

After creation, inspect the current status and available transitions. Preview and approve the configured ready transition before executing it. Move only when Jira offers that transition; never skip required states.

## 8. Start implementation behind the ticket

Use the configured branch template and include the Jira key. Create an isolated branch or worktree. Immediately before the first edit, inspect available transitions. Preview the configured in-progress transition, obtain explicit approval, then execute it through MCP or REST fallback:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json status <ISSUE-KEY>
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json transition <ISSUE-KEY> --to "<configured in-progress status>"
python3 "<skill-dir>/scripts/jira_workflow.py" --config .jira-ticket-workflow.json transition <ISSUE-KEY> --to "<configured in-progress status>" --write
```

Keep one ticket and pull request focused on one outcome. Add a failing regression test or evaluation before or with a fix whenever practical.

For a new agent, define ownership, positive and negative routing examples, inputs, tools, permissions, confirmation gates, output contract, failure behavior, tests or evaluations, and root routing.

## 9. Track plan deviations

Track the approved plan, territory discovered later, the deviation and reason, and its effect on scope, assumptions, acceptance criteria, risk, and tests.

For a small change, preserve this in the Jira progress comment and pull request. For full or multi-session work, maintain temporary implementation notes and move durable conclusions into existing repository documentation before handoff.

Stop and obtain renewed approval if a deviation materially changes approved scope or risk.

## 10. Validate, explain, and open the pull request

Run relevant tests, inspect the diff, and write the pull request in two layers:

- Keep the title, reason, behavior summary, operational impact, and rollback understandable without reading code.
- Put exact files, functions, queries, conditions, commands, and results in implementation, validation, deviation, and review sections.

After implementation and validation, but before opening the pull request:

1. Explain the previous behavior, new behavior, reason, operational effect, and review focus in plain language.
2. Use one structured question when available, or ask directly in chat, to check understanding of behavior, impact, risk, or rollback—not implementation trivia.
3. Explain the reasoning plainly after the answer. Resolve any surfaced mismatch before opening the pull request.
4. Treat this as a handoff aid, not a score or gate. If the user skips or does not answer, continue with the pull request.

Preview the Jira progress comment with the team-readable summary, validation result, material plan deviations, residual risk, and pull-request link. Obtain explicit approval, then write it through MCP or REST fallback.

Open the pull request against `pull_request.base_branch`. Inspect the created
pull request and confirm its actual base branch matches the configuration. If it
does not, stop handoff until the new target branch's existence and merge controls
pass readiness.

For `automated` status sync, Jira Automation owns the review lifecycle:

- `Pull request created` moves the linked ticket from the configured in-progress
  status to the configured in-review status.
- `Pull request merged` moves the linked ticket from in-review to done.
- An optional `Pull request declined` rule may return the ticket to in-progress.

After opening the pull request, verify that it appears in Jira's Development
panel and Jira reached in-review. If either check fails, inspect the rule audit
log and report the failure; do not silently force the transition. Never approve
or merge the pull request for the user. GitHub branch protection and merge
permissions, not the Jira merged event, must enforce human approval. If policy
requires a person to perform the merge action, also verify that auto-merge is
disabled and GitHub Apps cannot merge or bypass the rule.

For `manual` status sync, preview and approve the in-review transition after the
pull request opens, and report the done transition required after the approved
merge.
