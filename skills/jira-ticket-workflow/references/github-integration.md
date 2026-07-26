# Jira–GitHub status synchronization

Use this guide when `pull_request.jira_status_sync` is `automated`. Automated
status sync is a required setup contract, not a best-effort enhancement.

## Contents

- [Required outcome](#required-outcome)
- [Preflight result states](#preflight-result-states)
- [Read-only preflight](#read-only-preflight)
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
- An active GitHub ruleset or branch protection rule requires a pull request and
  at least one approving human review on the target branch. Review bypass is
  limited to explicitly approved actors. Jira's merged trigger only proves that
  the pull request was merged.

## Preflight result states

Classify each required check explicitly:

- `pass`: evidence confirms the configured behavior.
- `missing`: the app, connection, repository, rule, status, or transition is absent.
- `misconfigured`: the object exists but points to the wrong scope, status, or actor.
- `unverified`: current credentials or tools cannot inspect it.

For automated mode, every required structural result other than `pass` blocks
implementation. Never turn `unverified` into `missing`, and never accept a
configuration field or a user-authored boolean as proof. An end-to-end event
test may be deferred until the first real pull request when no safe existing
example exists.

## Read-only preflight

Prefer an authoritative connector or API. Use an authenticated browser session
when app-management or Automation details are not exposed by the connector. Do
not install apps, connect organizations, edit repository access, or create rules
without the user's explicit approval.

Verify all of the following:

1. **Target repository**
   - Resolve the GitHub owner and repository from `origin`.
   - Confirm the active GitHub account can read the repository and has the access
     needed for the planned branch and pull request.
2. **GitHub for Atlassian**
   - In Jira, open **Apps → Manage your apps → GitHub for Atlassian**.
   - Confirm the app is installed and the target GitHub organization is listed as
     connected.
   - Confirm the target repository is included when the installation uses
     selected-repository access.
3. **Jira-key linkage**
   - Confirm the branch and pull-request title templates contain `{ticket}` and
     produce an uppercase Jira key.
   - When a safe existing example exists, confirm its pull request appears in the
     ticket's Development panel.
   - When no example exists, record the Development-panel test as deferred rather
     than unverified. Verify it immediately after the first real pull request and
     block handoff if the pull request does not appear or the ticket does not
     reach in-review.
4. **Automation rules**
   - Inspect Automation through its read-only API or Jira UI.
   - Confirm an enabled `Pull request created` rule transitions the linked ticket
     from the configured in-progress status to the configured in-review status.
   - Confirm an enabled `Pull request merged` rule transitions the linked ticket
     from the configured in-review status to the configured done status.
   - If configured, confirm `Pull request declined` returns the ticket to
     in-progress.
   - Check rule scope, project, conditions, destination status, and actor. A
     matching rule name alone is not evidence.
5. **Workflow and permissions**
   - Confirm the issue type's workflow contains these exact paths:
     - configured ready → configured in-progress;
     - configured in-progress → configured in-review;
     - configured in-review → configured done;
     - configured in-review → configured in-progress when the declined rule is enabled.
   - Confirm the Automation actor has permission to transition tickets in the
     configured project.
   - Confirm users who need to inspect linked pull requests have View Development
     Tools permission.
6. **GitHub merge controls**
   - Inspect the active ruleset or branch protection rule for the pull request's
     target branch.
   - Require changes through pull requests and at least one approving human review.
   - Inspect bypass actors and confirm bots or broad roles cannot bypass review
     unless the user explicitly accepts that exception.
   - If the policy literally requires a person to perform the merge, confirm
     auto-merge is disabled and GitHub Apps cannot merge or bypass the rule.

After every required structural item passes and any first-event test is
explicitly recorded as deferred, rerun the helper with:

```text
python3 "<skill-dir>/scripts/jira_workflow.py" \
  --config .jira-ticket-workflow.json \
  check --repo . --jira-connection <mcp-or-rest> \
  --verified-external-check jira_github_connection \
  --verified-external-check jira_automation_rules \
  --verified-external-check jira_workflow_automation \
  --verified-external-check github_merge_controls
```

When Jira was verified through MCP, also add:

```text
--verified-external-check jira_mcp
```

The flags record checks already completed by the host for that invocation. They
must not be passed speculatively or saved in repository configuration.

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

### Protect the target branch

In the repository or organization rulesets, target the branch that receives the
pull request:

1. Enable **Require a pull request before merging**.
2. Require at least one approving review.
3. Prefer dismissing stale approvals or requiring approval of the latest push.
4. Review bypass permissions and remove unintended GitHub Apps, roles, or teams.
5. Disable auto-merge when a person must perform the merge action.

After the first real pull request opens, confirm that it appears in Jira's
Development panel, its actual base branch matches `pull_request.base_branch`,
and the ticket reaches in-review. Block handoff if any check fails. A different
base branch requires a fresh merge-control inspection before handoff. After the
approved merge, confirm that the ticket reaches done and inspect the Jira
Automation audit log if the transition fails.

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
- Merge controls missing: guide a repository administrator to require a pull
  request and approving review, then review bypass actors.
- Inspection denied: report `unverified`, name the missing role, and give the admin
  the checklist above.

Do not fall back silently to manual status changes when configuration requires
automated sync. The user may deliberately change `jira_status_sync` to `manual`
through the normal configuration preview and approval flow.

## Authoritative references

- [Connect GitHub Cloud to Jira](https://support.atlassian.com/jira-cloud-administration/docs/integrate-with-github-cloud/)
- [Link GitHub development information to Jira work items](https://support.atlassian.com/jira-cloud-administration/docs/use-the-github-for-jira-app/)
- [Jira Automation DevOps triggers](https://support.atlassian.com/cloud-automation/docs/jira-automation-triggers/)
- [Automation REST API](https://developer.atlassian.com/cloud/automation/rest/intro/)
- [Available GitHub ruleset rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
