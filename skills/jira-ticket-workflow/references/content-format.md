# Jira and Pull-Request Content

Use the same approved facts across Jira and the pull request, but write each artifact for its reader.

## Technical depth

| Content | Reader | Technical depth |
|---|---|---|
| Internal discovery | Implementing agent | Map, territory, unknown matrix, prototypes, and investigation detail |
| Jira summary, background, problem or need, expected behavior, acceptance criteria | Whole team | Observable behavior, operational impact, and desired outcome |
| Jira current behavior, cause, scope, validation, risk, rollback | Agent owner or engineer | Component and condition detail only when it helps approval or future diagnosis |
| Pull-request summary, impact, rollback | Whole team | Understandable without reading code |
| Pull-request implementation, validation, deviations, review focus | Reviewer | Exact files, functions, queries, commands, and results |

Preserve exact system identifiers such as agent names, status values, tables, fields, API labels, and error codes. Begin technical explanations with the observable effect, then give the minimum implementation detail needed.

Do not put raw prompts, the map/territory framework, the unknown matrix, full logs, or a file-by-file implementation diary into Jira.

## Approval packet

For a routine fix, show:

1. Diagnosis and reproduction
2. Consequential assumptions and non-goals
3. Duplicate-search result
4. Proposed Jira summary and body
5. One explicit approval question

Use the host's structured question tool when available. Otherwise ask directly in chat and accept only a clear confirmation of the exact proposed Jira write.

For a new agent or broad change, add ownership rationale, territory inspected, consequential unknowns, resolved decisions, references, and remaining non-blocking questions.

## Ticket input

Write this JSON to a temporary path outside the repository. All fields shown below are required except `decisions_and_assumptions`, `non_goals`, `related_issues`, `references`, and `priority`.

```json
{
  "change_type": "fix",
  "agent": "notification-agent",
  "title": "Distinguish expected delay from delivery failure",
  "discovery_depth": "light",
  "background": "Delivery confirmation can arrive after the provider accepts a message.",
  "problem_or_need": "Messages waiting for normal confirmation are reported as failures.",
  "current_behavior": "Every unconfirmed message is labeled as failed.",
  "expected_behavior": "Expected confirmation delay is distinct from an actual delivery failure.",
  "evidence": "Sanitized reproduction and observed timing.",
  "cause_or_need": "The classifier has no state for the expected confirmation window.",
  "scope": [
    "Notification status classification",
    "User-facing wording",
    "Regression coverage"
  ],
  "decisions_and_assumptions": [
    "Keep the existing output structure."
  ],
  "non_goals": [
    "Redesign the provider retry policy."
  ],
  "acceptance_criteria": [
    "Expected confirmation delay is not labeled as failure.",
    "Actual failures and existing successful cases keep their behavior."
  ],
  "validation_plan": "Test both sides of the timing boundary and existing fixtures.",
  "impact_and_risk": "Failure counts may decrease; timestamp parsing is the main regression risk.",
  "rollback": "Restore the previous classifier and rerun regression tests.",
  "related_issues": ["ENG-100"],
  "references": ["docs/notification-agent.md"],
  "priority": "Medium",
  "discovery_confirmed": true,
  "material_decisions_resolved": true,
  "duplicate_search_confirmed": true
}
```

The three discovery flags are enforced by the helper. Human approval remains a conversation-level gate and is not represented as a spoofable JSON boolean. The helper previews writes by default; use `--write` only after the user approves the exact preview. Discovery details themselves are intentionally not persisted in the Jira description.

## Jira description

The helper produces these sections:

```markdown
### Target

### Background

### Problem
<!-- "New capability" for a new agent -->

### Current behavior

### Expected behavior

### Evidence

### Cause
<!-- "Capability gap" for a new agent -->

### Scope

### Decisions and assumptions
<!-- Omitted when empty -->

### Out of scope
<!-- Omitted when empty -->

### Acceptance criteria

### Validation plan

### Impact and risk

### Rollback

### Related work and references
<!-- Omitted when empty -->
```

## Pull-request body

Use the target repository's template when present. Otherwise use:

```markdown
## Related Jira

## Change summary
<!-- Plain-language behavior and reason -->

## Implementation
<!-- Exact components, files, functions, queries, and decisions -->

## Validation results
<!-- Commands, outcomes, boundaries, and regression cases -->

## Impact and risk
<!-- Operational effect first, technical evidence second -->

## Changes from the approved plan
<!-- Write "None" when there were no deviations -->

## Rollback

## Review focus
<!-- Files, behavior, residual risk, and non-blocking questions -->
```

Jira contains the validation plan. The pull request contains the actual commands and results.

## Plain-language handoff

Before opening the pull request, explain:

- what users or operators observed before;
- what they will observe now;
- why the behavior changed;
- the operational effect and remaining risk;
- what the reviewer should confirm.

Then ask one understanding question about resulting behavior, impact, risk, or rollback. Do not test file names, function names, code syntax, or implementation trivia. Explain the reasoning after the response. Treat the check as optional handoff support, not a score or approval gate.
