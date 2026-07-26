# Jira–GitHub status synchronization

Use this guide when `pull_request.jira_status_sync` is `automated` and the user
asks for setup help or an observed PR-driven transition fails. Do not require
advance proof of every integration detail before implementation.

## Contents

- [Required outcome](#required-outcome)
- [Minimal preflight](#minimal-preflight)
- [Event-time verification](#event-time-verification)
- [Failure diagnosis](#failure-diagnosis)
- [Setup guide](#setup-guide)
- [Guided remediation](#guided-remediation)
- [Authoritative references](#authoritative-references)

## Required outcome

The integration must support this lifecycle:

```text
To Do → In Progress → In Review → Done
                       └─ PR declined → In Progress (optional)
```

- The skill moves a selected ticket to in-progress before implementation.
- Jira Automation moves a linked ticket to in-review when a pull request is
  created.
- Jira Automation moves the ticket to done when the pull request is merged.
- The integration observes pull-request events. Repository review and merge
  policies are owned separately and are not status-sync readiness requirements.

## Minimal preflight

Before implementation, verify only what is needed to work safely:

- Jira identity, configured project access, and search;
- preservation of existing Git changes and the configured base branch;
- GitHub authentication and target repository access when GitHub opens the PR;
- an uppercase Jira key in the branch and pull-request title templates.

A dirty primary checkout is not automatically a blocker when a clean task
worktree can be created without altering it. Never stash, reset, clean, or
overwrite existing work merely to make readiness pass.

Do not block implementation because the host cannot inspect the Marketplace app,
repository selection, Automation rules, workflow paths, Development panel, or
Automation actor. Do not open administrator settings solely to make readiness
pass. If reliable read-only evidence is already available, record it; otherwise
leave these checks in `deferred_checks`.

For MCP, attest only the core Jira access check when rerunning the helper:

```bash
python3 "<skill-dir>/scripts/jira_workflow.py" \
  --config .jira-ticket-workflow.json \
  check --repo . --jira-connection mcp \
  --verified-external-check jira_mcp
```

There are no status-sync attestation flags. The helper reports the created and
merged checks as per-run event-time reminders rather than preflight
requirements. Record successful event verification in the handoff context; the
helper does not maintain durable integration state.

## Event-time verification

### After pull-request creation

1. Confirm the actual base branch matches `pull_request.base_branch`.
2. Confirm the branch or PR title includes the uppercase Jira key.
3. Inspect the linked ticket:
   - if the PR appears in Development and the ticket reaches in-review, continue;
   - if either result is missing, keep the PR open and diagnose that event.
4. Report the observed failure and the next corrective step. Do not silently
   force a transition.
5. When useful, offer a one-time manual in-review transition through the normal
   preview and approval flow.

The missing automatic transition does not invalidate completed code or require
closing the PR. Keep the integration problem visible in the handoff.

### After merge

When the user reports a merge or asks for cleanup, confirm the linked ticket
reaches done. If it does not, diagnose the merged-event path and offer a one-time
manual done transition through the normal approval flow. Repeated failures
should lead to fixing Automation or explicitly changing
`jira_status_sync` to `manual`.

## Failure diagnosis

Inspect only the path associated with the failed event:

1. **PR absent from Development**
   - Check the uppercase Jira key in the branch and PR title.
   - Check whether GitHub for Atlassian is installed and the organization is
     connected.
   - Check whether selected-repository access includes the target repository.
2. **PR present but ticket did not reach in-review**
   - Inspect the `Pull request created` rule's enabled state, scope, condition,
     and transition.
   - Confirm the workflow offers in-progress → in-review.
   - Confirm the Automation actor has Transition issues permission.
   - Inspect the Automation audit log for the event.
3. **Merged PR did not move the ticket to done**
   - Inspect the `Pull request merged` rule, its status condition, and audit log.
   - Confirm the workflow offers in-review → done.
   - Confirm the Automation actor can perform that transition.
4. **Inspection unavailable**
   - Report the result as unverified, name the administrator role needed, and
     give the relevant checklist below. Do not turn missing admin access into a
     repository implementation blocker.

Prefer an authoritative connector or API. Use an authenticated browser session
only when the user asks for guided setup or failure diagnosis and the connector
cannot expose the required setting. Do not install apps, connect organizations,
edit repository access, or create rules without explicit approval.

## Setup guide

### Install and connect GitHub for Atlassian

Required roles:

- Jira site administrator
- GitHub organization owner

In Jira:

1. Open **Apps → Explore more apps**.
2. Find **GitHub for Atlassian**.
3. Select **Get app → Get it now**.
4. Open **Get started**. For an existing installation, use
   **Apps → Manage your apps → GitHub for Atlassian**.
5. Select **Continue → GitHub Cloud → Next** and sign in to GitHub.
6. Select the target organization and choose **Connect**.

Only a GitHub organization owner can complete the connection. If `Connect` is
not available, use **Send them a link and ask them to connect** and have an owner
approve it.

When GitHub asks for repository access, choose either all repositories or only
selected repositories. For selected access, include the target repository. To
add it later:

1. Open **Apps → Manage apps → GitHub for Atlassian → Configure**.
2. Open the organization's menu and choose **Configure**.
3. In GitHub, add the repository under **Repository access** and save.

Return to the Jira app page and confirm that the organization is connected.

### Link pull requests to tickets

Keep the uppercase Jira key in both the source branch and pull-request title:

```text
branch: ENG-123-short-description
PR:     ENG-123 Improve status synchronization
```

The configured Git templates must preserve `{ticket}`. A comment containing a
ticket URL is useful for humans but does not replace Development data linkage.

### Create the Automation rules

Create project-scoped rules unless the team intentionally needs a global scope.

Rule 1:

```text
Trigger: Pull request created
Condition: linked ticket is In Progress
Action: Transition ticket to In Review
```

Rule 2:

```text
Trigger: Pull request merged
Condition: linked ticket is In Review
Action: Transition ticket to Done
```

Optional Rule 3:

```text
Trigger: Pull request declined
Condition: linked ticket is In Review
Action: Transition ticket to In Progress
```

Enable each rule, confirm its actor, and inspect the audit log after the first
real event. If an automatic transition fails, first check for a missing workflow
transition and missing Transition issues permission.

After the first real pull request opens, confirm that it appears in Jira's
Development panel, its actual base branch matches `pull_request.base_branch`,
and the ticket reaches in-review. If it fails, follow the event-time diagnosis
above without discarding completed implementation. After the merge, confirm
that the ticket reaches done and inspect the Jira Automation audit log if the
transition fails.

## Guided remediation

Give the shortest path for the observed failure:

- App missing: provide the install steps and required administrators.
- Organization missing: guide the GitHub owner through Connect or the approval link.
- Repository excluded: guide the owner to Repository access.
- Development data missing: verify uppercase Jira keys and repository selection.
- Rule missing or disabled: guide a Jira project admin through the exact trigger,
  condition, and transition action.
- Transition missing: guide a Jira admin to add the workflow path.
- Actor denied: identify the configured actor and request Transition issues permission.
- Inspection denied: report `unverified`, name the missing role, and give the admin
  the checklist above.

Do not fall back silently to manual status changes. Offer a one-time transition
only with an exact preview and explicit approval. For repeated manual ownership,
change `jira_status_sync` to `manual` through the normal configuration preview
and approval flow.

## Authoritative references

- [Connect GitHub Cloud to Jira](https://support.atlassian.com/jira-cloud-administration/docs/integrate-with-github-cloud/)
- [Link GitHub development information to Jira work items](https://support.atlassian.com/jira-cloud-administration/docs/use-the-github-for-jira-app/)
- [Jira Automation DevOps triggers](https://support.atlassian.com/cloud-automation/docs/jira-automation-triggers/)
- [Automation REST API](https://developer.atlassian.com/cloud/automation/rest/intro/)
