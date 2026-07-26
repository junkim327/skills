#!/usr/bin/env python3
"""Configurable Jira Cloud I/O for the public jira-ticket-workflow skill.

The script is intentionally stdlib-only. It validates workflow gates, builds Jira
ADF payloads, previews writes, and performs only the Jira operation requested by
the caller. Diagnosis and policy decisions remain the agent's responsibility.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


VALID_CHANGE_TYPES = {"fix", "new"}
VALID_DISCOVERY_DEPTHS = {"light", "full"}
VALID_CC_MODES = {"none", "field", "comment"}
VALID_PR_PROVIDERS = {"github", "manual"}
VALID_JIRA_STATUS_SYNC_MODES = {"automated", "manual"}
VALID_JIRA_CONNECTIONS = {"mcp", "rest"}
AUTOMATED_STATUS_SYNC_CHECKS = (
    "jira_github_connection",
    "jira_automation_rules",
    "jira_workflow_automation",
    "github_merge_controls",
)
VALID_EXTERNAL_CHECKS = {"jira_mcp", *AUTOMATED_STATUS_SYNC_CHECKS}
CONFIG_VERSION = 2
DEFAULT_CONFIG_NAME = ".jira-ticket-workflow.json"
ISSUE_KEY_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-([1-9][0-9]*)$")
REQUIRED_TICKET_FIELDS = (
    "change_type",
    "agent",
    "title",
    "discovery_depth",
    "background",
    "problem_or_need",
    "current_behavior",
    "expected_behavior",
    "evidence",
    "cause_or_need",
    "scope",
    "acceptance_criteria",
    "validation_plan",
    "impact_and_risk",
    "rollback",
)
RESERVED_EXTRA_FIELDS = {
    "project",
    "issuetype",
    "summary",
    "description",
    "labels",
    "components",
    "priority",
    "assignee",
}


class JiraError(RuntimeError):
    """A Jira configuration, authentication, or API failure."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent authorization headers from following Jira redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _fail(message: str) -> None:
    raise ValueError(message)


def _load_object(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"file not found: {source}")
    except json.JSONDecodeError as exc:
        _fail(f"invalid JSON in {source}: {exc}")
    if not isinstance(value, dict):
        _fail(f"JSON root must be an object: {source}")
    return value


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in items) or default
    rendered = str(value).strip()
    return rendered or default


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    if not slug:
        _fail("agent must contain an ASCII slug, for example notification-agent")
    return slug[:80]


def _safe_search_phrase(value: str) -> str:
    cleaned = re.sub(r"[^\w\s-]+", " ", value, flags=re.UNICODE)
    return " ".join(cleaned.split())[:120]


def _jql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validated_issue_key(config: dict[str, Any], value: Any) -> str:
    issue = str(value or "").strip()
    match = ISSUE_KEY_PATTERN.fullmatch(issue)
    if not match:
        _fail("issue must be a Jira key such as ENG-123")
    configured_project = str(config["jira"]["project_key"]).strip()
    if match.group(1).casefold() != configured_project.casefold():
        _fail(
            f"issue {issue} is outside configured Jira project {configured_project}"
        )
    return issue


def _validated_target_status(config: dict[str, Any], value: Any) -> str:
    target = str(value or "").strip()
    allowed = {
        str(status).strip().casefold(): str(status).strip()
        for status in config["jira"]["statuses"].values()
    }
    selected = allowed.get(target.casefold())
    if not selected:
        _fail(
            "transition target must be one of the configured Jira statuses: "
            + ", ".join(allowed.values())
        )
    return selected


def _redact_account_ids(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized == "accountid":
                redacted[key] = "<redacted>"
            elif normalized == "account_ids" and isinstance(child, list):
                redacted[key] = ["<redacted>" for _ in child]
            else:
                redacted[key] = _redact_account_ids(child)
        return redacted
    if isinstance(value, list):
        return [_redact_account_ids(child) for child in value]
    return value


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _jira_origin(value: Any) -> str:
    rendered = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(rendered)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not hostname:
        _fail("jira.base_url must be an https URL")
    if hostname != "atlassian.net" and not hostname.endswith(".atlassian.net"):
        _fail("jira.base_url must use a trusted *.atlassian.net Jira Cloud host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        _fail("jira.base_url must not contain credentials, query parameters, or fragments")
    return f"https://{parsed.netloc}"


def _template_errors(name: str, template: Any, *, require_ticket: bool) -> list[str]:
    if not isinstance(template, str) or not template.strip():
        return [f"{name} must be a non-empty string"]
    if require_ticket and "{ticket}" not in template:
        return [f"{name} must contain {{ticket}}"]
    try:
        template.format(
            ticket="ENG-123",
            agent="notification-agent",
            slug="short-description",
            change_type="fix",
            summary="Improve behavior",
        )
    except (KeyError, ValueError) as exc:
        return [f"invalid {name}: {exc}"]
    return []


def _placeholder_errors(config: dict[str, Any]) -> list[str]:
    jira = config.get("jira") if isinstance(config.get("jira"), dict) else {}
    values = {
        "jira.base_url": str(jira.get("base_url") or ""),
        "jira.project_key": str(jira.get("project_key") or ""),
    }
    errors: list[str] = []
    for name, value in values.items():
        normalized = value.casefold()
        if any(marker in normalized for marker in ("your-domain", "your-org", "your_company")):
            errors.append(f"{name} still contains an example placeholder")
        if "<" in value or ">" in value:
            errors.append(f"{name} still contains an example placeholder")
    return errors


def _contains_secret_keys(value: Any, *, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            normalized = str(key).casefold().replace("-", "_")
            if any(marker in normalized for marker in ("token", "password", "secret", "credential")):
                found.append(child_path)
            found.extend(_contains_secret_keys(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_contains_secret_keys(child, path=f"{path}[{index}]"))
    return found


def _unknown_key_errors(
    path: str,
    value: dict[str, Any],
    allowed: set[str],
) -> list[str]:
    unknown = sorted(set(value) - allowed)
    return [f"{path} contains unknown key: {key}" for key in unknown]


def _is_safe_relative_path(value: str) -> bool:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return (
        bool(value.strip())
        and not posix.is_absolute()
        and not windows.is_absolute()
        and ".." not in posix.parts
        and ".." not in windows.parts
    )


def _is_safe_branch_name(value: str) -> bool:
    rendered = str(value or "").strip()
    forbidden = set(" ~^:?*[\\")
    return (
        bool(rendered)
        and len(rendered) <= 255
        and rendered == value
        and not rendered.startswith(("-", ".", "/"))
        and not rendered.endswith((".", "/", ".lock"))
        and ".." not in rendered
        and "@{" not in rendered
        and "//" not in rendered
        and not any(character in forbidden or ord(character) < 32 for character in rendered)
    )


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    jira = config.get("jira")
    ticket = config.get("ticket")
    git = config.get("git")
    pull_request = config.get("pull_request")

    errors.extend(
        _unknown_key_errors(
            "configuration",
            config,
            {"version", "jira", "ticket", "git", "pull_request"},
        )
    )

    version = config.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        errors.append(f"version must be {CONFIG_VERSION}")

    if not isinstance(jira, dict):
        errors.append("jira must be an object")
        jira = {}
    else:
        errors.extend(
            _unknown_key_errors(
                "jira",
                jira,
                {
                    "base_url",
                    "project_key",
                    "issue_type",
                    "component",
                    "default_priority",
                    "assign_to_current_user",
                    "statuses",
                    "cc",
                },
            )
        )
    if not isinstance(ticket, dict):
        errors.append("ticket must be an object")
        ticket = {}
    else:
        errors.extend(
            _unknown_key_errors(
                "ticket",
                ticket,
                {
                    "summary_templates",
                    "labels",
                    "add_change_label",
                    "add_agent_label",
                    "extra_fields",
                },
            )
        )

    base_url = jira.get("base_url")
    try:
        _jira_origin(base_url)
    except ValueError as exc:
        errors.append(str(exc))
    override_url = os.environ.get("JIRA_BASE_URL")
    if override_url:
        try:
            _jira_origin(override_url)
        except ValueError as exc:
            errors.append(f"JIRA_BASE_URL: {exc}")

    project_key = jira.get("project_key")
    if not isinstance(project_key, str) or not project_key.strip():
        errors.append("jira.project_key must be a non-empty string")

    issue_type = jira.get("issue_type")
    if not isinstance(issue_type, dict):
        errors.append("jira.issue_type must be an object containing name or id")
    else:
        errors.extend(_unknown_key_errors("jira.issue_type", issue_type, {"name", "id"}))
        present = [key for key in ("name", "id") if str(issue_type.get(key) or "").strip()]
        if len(present) != 1:
            errors.append("jira.issue_type must contain exactly one of name or id")

    statuses = jira.get("statuses")
    if not isinstance(statuses, dict):
        errors.append("jira.statuses must be an object")
    else:
        errors.extend(
            _unknown_key_errors(
                "jira.statuses",
                statuses,
                {"ready", "in_progress", "in_review", "done"},
            )
        )
        for key in ("ready", "in_progress", "in_review", "done"):
            if not isinstance(statuses.get(key), str) or not statuses[key].strip():
                errors.append(f"jira.statuses.{key} must be a non-empty string")

    for key in ("component", "default_priority"):
        if jira.get(key) is not None and not isinstance(jira.get(key), str):
            errors.append(f"jira.{key} must be a string or null")
    if not isinstance(jira.get("assign_to_current_user", True), bool):
        errors.append("jira.assign_to_current_user must be a boolean")

    cc = jira.get("cc", {})
    if not isinstance(cc, dict):
        errors.append("jira.cc must be an object")
        cc = {}
    else:
        errors.extend(
            _unknown_key_errors(
                "jira.cc", cc, {"mode", "field_id", "comment_text", "account_ids"}
            )
        )
    mode = cc.get("mode", "none")
    if mode not in VALID_CC_MODES:
        errors.append("jira.cc.mode must be one of: none, field, comment")
    if mode == "field" and not str(cc.get("field_id") or "").strip():
        errors.append("jira.cc.field_id is required when mode is field")
    account_ids = cc.get("account_ids", [])
    if not isinstance(account_ids, list) or any(not isinstance(item, str) for item in account_ids):
        errors.append("jira.cc.account_ids must be an array of strings")
    else:
        unique_account_ids = _unique_strings(account_ids)
        if mode in {"field", "comment"} and not unique_account_ids:
            errors.append(f"jira.cc.account_ids must not be empty when mode is {mode}")
        elif mode == "none" and unique_account_ids:
            errors.append("jira.cc.account_ids must be empty when mode is none")
        elif len(unique_account_ids) > 100:
            errors.append("jira.cc.account_ids supports at most 100 unique accounts")
    if not isinstance(cc.get("comment_text", "CC"), str):
        errors.append("jira.cc.comment_text must be a string")

    templates = ticket.get("summary_templates")
    if not isinstance(templates, dict):
        errors.append("ticket.summary_templates must be an object")
    else:
        errors.extend(
            _unknown_key_errors(
                "ticket.summary_templates", templates, VALID_CHANGE_TYPES
            )
        )
        for change_type in sorted(VALID_CHANGE_TYPES):
            template = templates.get(change_type)
            if not isinstance(template, str) or not template.strip():
                errors.append(f"ticket.summary_templates.{change_type} must be a non-empty string")
                continue
            try:
                template.format(agent="agent", title="title")
            except (KeyError, ValueError) as exc:
                errors.append(f"invalid {change_type} summary template: {exc}")

    labels = ticket.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        errors.append("ticket.labels must be an array of strings")
    for key in ("add_change_label", "add_agent_label"):
        if not isinstance(ticket.get(key, True), bool):
            errors.append(f"ticket.{key} must be a boolean")

    extra_fields = ticket.get("extra_fields", {})
    if not isinstance(extra_fields, dict):
        errors.append("ticket.extra_fields must be an object")
        extra_fields = {}
    forbidden = RESERVED_EXTRA_FIELDS.intersection(extra_fields)
    field_id = str(cc.get("field_id") or "").strip()
    if field_id and field_id in extra_fields:
        forbidden.add(field_id)
    if forbidden:
        errors.append("ticket.extra_fields cannot override: " + ", ".join(sorted(forbidden)))

    if not isinstance(git, dict):
        errors.append("git must be an object")
        git = {}
    else:
        errors.extend(
            _unknown_key_errors(
                "git",
                git,
                {"fix_branch", "new_branch", "commit", "pull_request_title"},
            )
        )
    for key in ("fix_branch", "new_branch", "commit", "pull_request_title"):
        errors.extend(
            _template_errors(
                f"git.{key}",
                git.get(key),
                require_ticket=True,
            )
        )

    if not isinstance(pull_request, dict):
        errors.append("pull_request must be an object")
        pull_request = {}
    else:
        errors.extend(
            _unknown_key_errors(
                "pull_request",
                pull_request,
                {
                    "provider",
                    "jira_status_sync",
                    "base_branch",
                    "template_path",
                },
            )
        )
    provider = pull_request.get("provider", "github")
    if provider not in VALID_PR_PROVIDERS:
        errors.append("pull_request.provider must be one of: github, manual")
    jira_status_sync = pull_request.get("jira_status_sync", "automated")
    if jira_status_sync not in VALID_JIRA_STATUS_SYNC_MODES:
        errors.append(
            "pull_request.jira_status_sync must be one of: automated, manual"
        )
    base_branch = pull_request.get("base_branch")
    if not isinstance(base_branch, str) or not _is_safe_branch_name(base_branch):
        errors.append(
            "pull_request.base_branch must be a safe non-empty Git branch name"
        )
    template_path = pull_request.get("template_path")
    if template_path is not None and not isinstance(template_path, str):
        errors.append("pull_request.template_path must be a string or null")
    elif isinstance(template_path, str) and template_path and not _is_safe_relative_path(template_path):
        errors.append("pull_request.template_path must stay within the repository")

    if errors:
        _fail("invalid configuration:\n- " + "\n- ".join(errors))
    return config


def load_config(path: str | Path) -> dict[str, Any]:
    return validate_config(_load_object(path))


def _paragraphs(text: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for line in str(text or "").splitlines() or [""]:
        nodes = [{"type": "text", "text": line}] if line else []
        content.append({"type": "paragraph", "content": nodes})
    return content


def build_description_adf(sections: list[tuple[str, str]], *, level: int = 3) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for heading, body in sections:
        content.append(
            {
                "type": "heading",
                "attrs": {"level": level},
                "content": [{"type": "text", "text": heading}],
            }
        )
        content.extend(_paragraphs(body))
    return {"type": "doc", "version": 1, "content": content}


def build_comment_adf(text: str, *, mention_ids: list[str] | None = None) -> dict[str, Any]:
    content = _paragraphs(text)
    if mention_ids:
        mention_paragraph = {"type": "paragraph", "content": []}
        for index, account_id in enumerate(mention_ids):
            if index:
                mention_paragraph["content"].append({"type": "text", "text": " "})
            mention_paragraph["content"].append(
                {"type": "mention", "attrs": {"id": account_id, "text": ""}}
            )
        content.append(mention_paragraph)
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(adf_to_text(item) for item in node)
    if not isinstance(node, dict):
        return str(node)
    node_type = node.get("type")
    content = node.get("content", [])
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "mention":
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or f"@{attrs.get('id', 'user')}")
    if node_type in {"paragraph", "heading", "codeBlock"}:
        return adf_to_text(content) + "\n"
    if node_type in {"bulletList", "orderedList"}:
        return "".join(f"- {adf_to_text(item).strip()}\n" for item in content)
    return adf_to_text(content)


def _checkboxes(value: Any) -> str:
    items = _unique_strings(value) if isinstance(value, list) else []
    if not items:
        rendered = _text(value)
        items = [line.strip() for line in rendered.splitlines() if line.strip()]
    return "\n".join(item if item.startswith("[ ]") else f"[ ] {item}" for item in items)


def _validate_ticket(data: dict[str, Any]) -> None:
    if "approved" in data:
        _fail(
            "approved is no longer supported; preview the operation and use --write only after explicit human approval"
        )
    missing = [name for name in REQUIRED_TICKET_FIELDS if not _text(data.get(name))]
    if missing:
        _fail("missing required ticket fields: " + ", ".join(missing))
    if data.get("change_type") not in VALID_CHANGE_TYPES:
        _fail("change_type must be fix or new")
    if data.get("discovery_depth") not in VALID_DISCOVERY_DEPTHS:
        _fail("discovery_depth must be light or full")
    if data.get("discovery_confirmed") is not True:
        _fail("discovery_confirmed must be true")
    if data.get("material_decisions_resolved") is not True:
        _fail("material_decisions_resolved must be true")
    if data.get("duplicate_search_confirmed") is not True:
        _fail("duplicate_search_confirmed must be true")


def _cc_plan(config: dict[str, Any], requester_id: str | None) -> dict[str, Any]:
    cc = config["jira"].get("cc", {})
    mode = cc.get("mode", "none")
    account_ids = _unique_strings(cc.get("account_ids", []))
    if requester_id:
        account_ids = [account_id for account_id in account_ids if account_id != requester_id]

    plan: dict[str, Any] = {"mode": mode, "account_ids": account_ids}
    if mode == "field":
        plan["field_id"] = str(cc["field_id"])
        plan["field_value"] = [{"accountId": account_id} for account_id in account_ids]
    elif mode == "comment":
        plan["comment_adf"] = build_comment_adf(
            str(cc.get("comment_text") or "CC"), mention_ids=account_ids
        )
    return plan


def ticket_plan(
    config: dict[str, Any], data: dict[str, Any], *, requester_id: str | None = None
) -> dict[str, Any]:
    validate_config(config)
    _validate_ticket(data)

    change_type = data["change_type"]
    agent_slug = _slug(data["agent"])
    template = config["ticket"]["summary_templates"][change_type]
    summary = template.format(agent=agent_slug, title=_text(data["title"]))
    problem_heading = "Problem" if change_type == "fix" else "New capability"
    cause_heading = "Cause" if change_type == "fix" else "Capability gap"

    sections: list[tuple[str, str]] = [
        ("Target", f"Agent: {agent_slug}\nChange type: {change_type}"),
        ("Background", _text(data["background"])),
        (problem_heading, _text(data["problem_or_need"])),
        ("Current behavior", _text(data["current_behavior"])),
        ("Expected behavior", _text(data["expected_behavior"])),
        ("Evidence", _text(data["evidence"])),
        (cause_heading, _text(data["cause_or_need"])),
        ("Scope", _text(data["scope"])),
    ]

    decisions = _text(data.get("decisions_and_assumptions"))
    if decisions:
        sections.append(("Decisions and assumptions", decisions))
    non_goals = _text(data.get("non_goals"))
    if non_goals:
        sections.append(("Out of scope", non_goals))

    sections.extend(
        [
            ("Acceptance criteria", _checkboxes(data["acceptance_criteria"])),
            ("Validation plan", _text(data["validation_plan"])),
            ("Impact and risk", _text(data["impact_and_risk"])),
            ("Rollback", _text(data["rollback"])),
        ]
    )

    related = _text(data.get("related_issues"))
    references = _text(data.get("references"))
    related_parts = []
    if related:
        related_parts.append(f"Related issues:\n{related}")
    if references:
        related_parts.append(f"References:\n{references}")
    if related_parts:
        sections.append(("Related work and references", "\n".join(related_parts)))

    jira = config["jira"]
    fields: dict[str, Any] = {
        "project": {"key": jira["project_key"]},
        "issuetype": dict(jira["issue_type"]),
        "summary": summary,
        "description": build_description_adf(sections),
    }

    labels = _unique_strings(config["ticket"].get("labels", []))
    if config["ticket"].get("add_change_label", True):
        labels.append(f"change-{change_type}")
    if config["ticket"].get("add_agent_label", True):
        labels.append(f"agent-{agent_slug}")
    fields["labels"] = _unique_strings(labels)

    component = jira.get("component")
    if isinstance(component, str) and component.strip():
        fields["components"] = [{"name": component.strip()}]
    priority = data.get("priority") or jira.get("default_priority")
    if isinstance(priority, str) and priority.strip():
        fields["priority"] = {"name": priority.strip()}
    if requester_id and jira.get("assign_to_current_user", True):
        fields["assignee"] = {"accountId": requester_id}

    fields.update(dict(config["ticket"].get("extra_fields", {})))

    cc_plan = _cc_plan(config, requester_id)
    if cc_plan["mode"] == "field" and cc_plan["account_ids"]:
        fields[cc_plan["field_id"]] = cc_plan["field_value"]

    return {
        "summary": summary,
        "fields": fields,
        "cc": cc_plan,
        "discovery_depth": data["discovery_depth"],
    }


class JiraClient:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        base_url: str | None = None,
        timeout: int = 30,
    ):
        jira = (config or {}).get("jira") or {}
        configured_url = jira.get("base_url")
        environment_url = os.environ.get("JIRA_BASE_URL")
        if config is not None:
            if not configured_url:
                raise JiraError("config-backed Jira operations require jira.base_url")
            configured_origin = _jira_origin(configured_url)
            requested_override = base_url or environment_url
            if requested_override and _jira_origin(requested_override) != configured_origin:
                raise JiraError(
                    "JIRA_BASE_URL does not match the approved jira.base_url; refusing Jira operation"
                )
            selected_url = configured_origin
        else:
            selected_url = base_url or environment_url
        email = os.environ.get("JIRA_EMAIL")
        token = os.environ.get("JIRA_API_TOKEN")
        if not selected_url or not email or not token:
            raise JiraError(
                "live Jira operations require JIRA_EMAIL, JIRA_API_TOKEN, and "
                "jira.base_url or JIRA_BASE_URL"
            )
        self.base_url = _jira_origin(selected_url) + "/rest/api/3"
        self.timeout = timeout
        self.auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.ssl_context = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ssl_context),
            NoRedirectHandler(),
        )

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "Authorization": f"Basic {self.auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(body)
                message = "; ".join(parsed.get("errorMessages", []))
                if not message:
                    message = json.dumps(parsed.get("errors", {}))
            except json.JSONDecodeError:
                message = body
            raise JiraError(f"Jira HTTP {exc.code}: {message or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise JiraError(f"Jira connection failed: {exc.reason}") from exc

    def me(self) -> dict[str, Any]:
        return self._call("GET", "/myself")

    def projects(self, *, limit: int = 100) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"maxResults": max(1, min(limit, 100)), "orderBy": "name"}
        )
        result = self._call("GET", f"/project/search?{query}")
        return result.get("values", [])

    def project(self, project_key: str) -> dict[str, Any]:
        return self._call("GET", f"/project/{urllib.parse.quote(project_key)}")

    def issue_types(self, project_key: str, *, limit: int = 100) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"maxResults": max(1, min(limit, 100))})
        result = self._call(
            "GET",
            f"/issue/createmeta/{urllib.parse.quote(project_key)}/issuetypes?{query}",
        )
        return result.get("issueTypes") or result.get("values", [])

    def create_fields(
        self,
        project_key: str,
        issue_type_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"maxResults": max(1, min(limit, 200))})
        result = self._call(
            "GET",
            "/issue/createmeta/"
            f"{urllib.parse.quote(project_key)}/issuetypes/"
            f"{urllib.parse.quote(str(issue_type_id))}?{query}",
        )
        return result.get("fields", [])

    def project_statuses(self, project_key: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/project/{urllib.parse.quote(project_key)}/statuses")

    def permissions(self, project_key: str, permission_keys: list[str]) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"projectKey": project_key, "permissions": ",".join(permission_keys)}
        )
        result = self._call("GET", f"/mypermissions?{query}")
        return result.get("permissions", {})

    def fields(self) -> list[dict[str, Any]]:
        result = self._call("GET", "/field")
        return result if isinstance(result, list) else []

    def users(self, account_ids: list[str]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            [("accountId", account_id) for account_id in account_ids]
            + [("maxResults", str(max(1, min(len(account_ids), 100))))]
        )
        result = self._call("GET", f"/user/bulk?{query}")
        return result.get("values", [])

    def priorities(self) -> list[dict[str, Any]]:
        result = self._call("GET", "/priority")
        return result if isinstance(result, list) else []

    def components(self, project_key: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/project/{urllib.parse.quote(project_key)}/components")

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        result = self._call("POST", "/issue", {"fields": fields})
        key = result.get("key")
        return {
            "key": key,
            "id": result.get("id"),
            "url": f"{self.base_url.removesuffix('/rest/api/3')}/browse/{key}",
        }

    def add_comment(self, issue: str, adf: dict[str, Any]) -> dict[str, Any]:
        encoded_issue = urllib.parse.quote(issue, safe="")
        result = self._call("POST", f"/issue/{encoded_issue}/comment", {"body": adf})
        return {"id": result.get("id")}

    def search(self, jql: str, *, fields: str, limit: int) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        token: str | None = None
        while len(issues) < limit:
            query: dict[str, Any] = {
                "jql": jql,
                "fields": fields,
                "maxResults": min(100, limit - len(issues)),
            }
            if token:
                query["nextPageToken"] = token
            result = self._call("GET", "/search/jql?" + urllib.parse.urlencode(query))
            issues.extend(result.get("issues", []))
            token = result.get("nextPageToken")
            if not token:
                break
        return issues[:limit]

    def issue_status(self, issue: str) -> str | None:
        query = urllib.parse.urlencode({"fields": "status"})
        encoded_issue = urllib.parse.quote(issue, safe="")
        result = self._call("GET", f"/issue/{encoded_issue}?{query}")
        return ((result.get("fields") or {}).get("status") or {}).get("name")

    def transitions(self, issue: str) -> list[dict[str, Any]]:
        encoded_issue = urllib.parse.quote(issue, safe="")
        result = self._call("GET", f"/issue/{encoded_issue}/transitions")
        return result.get("transitions", [])

    def transition(self, issue: str, transition_id: str) -> None:
        encoded_issue = urllib.parse.quote(issue, safe="")
        self._call(
            "POST",
            f"/issue/{encoded_issue}/transitions",
            {"transition": {"id": str(transition_id)}},
        )


def _issue_summary(issue: dict[str, Any], matched_by: list[str]) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "resolution": (fields.get("resolution") or {}).get("name"),
        "updated": fields.get("updated"),
        "description_excerpt": adf_to_text(fields.get("description")).strip()[:600],
        "matched_by": matched_by,
    }


def _transition_preview(
    issue: str, target: str, transitions: list[dict[str, Any]]
) -> dict[str, Any]:
    wanted = target.strip().casefold()
    matches = [
        transition
        for transition in transitions
        if str((transition.get("to") or {}).get("name") or "").strip().casefold()
        == wanted
    ]
    available = [
        {
            "id": str(transition.get("id")),
            "name": transition.get("name"),
            "to": (transition.get("to") or {}).get("name"),
        }
        for transition in transitions
    ]
    if not matches:
        _fail(f"no transition to '{target}' from {issue}; available={available}")
    if len(matches) > 1:
        _fail(f"multiple transitions lead to '{target}' for {issue}; available={available}")
    selected = matches[0]
    return {
        "issue": issue,
        "target_status": (selected.get("to") or {}).get("name"),
        "transition_id": str(selected.get("id")),
        "transition_name": selected.get("name"),
        "available": available,
    }


def command_validate_config(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    jira = config["jira"]
    cc = jira.get("cc", {})
    return {
        "valid": True,
        "version": config.get("version", CONFIG_VERSION),
        "project_key": jira["project_key"],
        "issue_type": jira["issue_type"],
        "statuses": jira["statuses"],
        "cc_mode": cc.get("mode", "none"),
        "cc_account_count": len(_unique_strings(cc.get("account_ids", []))),
        "pull_request_provider": config["pull_request"].get("provider", "github"),
    }


def command_search(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    client = JiraClient(config)
    project = _jql_quote(config["jira"]["project_key"])
    agent_slug = _slug(args.agent)
    agent_phrase = _jql_quote(agent_slug.replace("-", " "))
    terms = _safe_search_phrase(args.terms or "")

    queries: list[tuple[str, str]] = [
        (
            "agent",
            f'project = "{project}" AND '
            f'(labels = "agent-{_jql_quote(agent_slug)}" OR summary ~ "\\"{agent_phrase}\\"") '
            "ORDER BY updated DESC",
        )
    ]
    if terms:
        exact = _jql_quote(terms)
        queries.append(
            ("exact_terms", f'project = "{project}" AND text ~ "\\"{exact}\\"" ORDER BY updated DESC')
        )
        tokens = []
        for token in terms.split():
            if len(token) >= 3 and token.casefold() not in {"agent", "error"}:
                tokens.append(_jql_quote(token))
        if tokens:
            broad = " OR ".join(f'text ~ "{token}"' for token in tokens[:6])
            queries.append(("broad_terms", f'project = "{project}" AND ({broad}) ORDER BY updated DESC'))

    fields = "summary,status,resolution,updated,description"
    by_key: dict[str, dict[str, Any]] = {}
    matched_by: dict[str, list[str]] = {}
    for query_name, jql in queries:
        for issue in client.search(jql, fields=fields, limit=args.limit):
            key = str(issue.get("key"))
            by_key.setdefault(key, issue)
            matched_by.setdefault(key, []).append(query_name)

    issues = [_issue_summary(issue, matched_by[key]) for key, issue in by_key.items()]
    return {
        "queries": [{"name": name, "jql": jql} for name, jql in queries],
        "count": len(issues),
        "issues": issues[: args.limit],
    }


def command_create(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    data = _load_object(args.input)
    write = bool(getattr(args, "write", False)) and not bool(
        getattr(args, "dry_run", False)
    )
    if not write:
        adjustments = []
        if config["jira"].get("assign_to_current_user", True):
            adjustments.append("live create assigns the authenticated Jira user")
        if config["jira"].get("cc", {}).get("account_ids"):
            adjustments.append("live create removes the authenticated Jira user from CC")
        plan = ticket_plan(config, data)
        return {
            "dry_run": True,
            "write_required": True,
            "summary": plan["summary"],
            "fields": _redact_account_ids(plan["fields"]),
            "cc": {
                "mode": plan["cc"]["mode"],
                "account_count": len(plan["cc"]["account_ids"]),
            },
            "discovery_depth": plan["discovery_depth"],
            "live_create_adjustments": adjustments,
        }

    client = JiraClient(config)
    me = client.me()
    requester_id = str(me.get("accountId") or "").strip() or None
    plan = ticket_plan(config, data, requester_id=requester_id)
    created = client.create_issue(plan["fields"])

    cc_result: dict[str, Any] | None = None
    if plan["cc"]["mode"] == "comment" and plan["cc"]["account_ids"]:
        try:
            cc_result = {
                "status": "added",
                **client.add_comment(created["key"], plan["cc"]["comment_adf"]),
            }
        except JiraError as exc:
            cc_result = {
                "status": "failed",
                "error": str(exc),
                "warning": "issue was created; add CC manually and do not rerun create",
            }

    result = {
        **created,
        "summary": plan["summary"],
        "labels": plan["fields"].get("labels", []),
        "cc": {
            "mode": plan["cc"]["mode"],
            "account_count": len(plan["cc"]["account_ids"]),
            "comment": cc_result,
        },
    }
    if cc_result and cc_result.get("status") == "failed":
        result["partial_success"] = True
    return result


def command_comment(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    issue = _validated_issue_key(config, args.issue)
    data = _load_object(args.input)
    text = _text(data.get("text"))
    if not text:
        _fail("comment input requires non-empty text")
    raw_mentions = data.get("mention_account_ids", [])
    if not isinstance(raw_mentions, list) or any(
        not isinstance(item, str) for item in raw_mentions
    ):
        _fail("mention_account_ids must be an array of strings")
    mention_ids = _unique_strings(raw_mentions)
    if len(mention_ids) > 100:
        _fail("mention_account_ids supports at most 100 unique accounts")
    configured_mentions = set(
        _unique_strings((config["jira"].get("cc") or {}).get("account_ids", []))
    )
    unapproved_mentions = [
        account_id for account_id in mention_ids if account_id not in configured_mentions
    ]
    if unapproved_mentions:
        _fail("mention_account_ids must be limited to configured Jira CC accounts")
    adf = build_comment_adf(text, mention_ids=mention_ids)
    write = bool(getattr(args, "write", False)) and not bool(
        getattr(args, "dry_run", False)
    )
    if not write:
        return {
            "dry_run": True,
            "write_required": True,
            "issue": issue,
            "adf": build_comment_adf(text),
            "mention_account_count": len(mention_ids),
        }
    result = JiraClient(config).add_comment(issue, adf)
    return {"issue": issue, **result}


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    issue = _validated_issue_key(config, args.issue)
    client = JiraClient(config)
    transitions = client.transitions(issue)
    return {
        "issue": issue,
        "status": client.issue_status(issue),
        "available_transitions": [
            {
                "id": str(transition.get("id")),
                "name": transition.get("name"),
                "to": (transition.get("to") or {}).get("name"),
            }
            for transition in transitions
        ],
    }


def command_transition(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    issue = _validated_issue_key(config, args.issue)
    target = _validated_target_status(config, args.to)
    client = JiraClient(config)
    preview = _transition_preview(issue, target, client.transitions(issue))
    write = bool(getattr(args, "write", False)) and not bool(
        getattr(args, "dry_run", False)
    )
    if not write:
        return {"dry_run": True, "write_required": True, **preview}
    client.transition(issue, preview["transition_id"])
    return {**preview, "transitioned": True}


def _check(
    check_id: str,
    state: str,
    message: str,
    *,
    required: bool,
    remediation: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "state": state,
        "required": required,
        "message": message,
    }
    if remediation:
        result["remediation"] = remediation
    return result


def _run_command(command: list[str], *, cwd: Path, timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout.strip()


def _remote_host(remote: str) -> str | None:
    value = remote.strip()
    if re.match(r"^[^@\s]+@[^:\s]+:", value):
        return value.split("@", 1)[1].split(":", 1)[0].casefold()
    parsed = urllib.parse.urlparse(value)
    return (parsed.hostname or "").casefold() or None


def _jira_failure_message(exc: JiraError) -> tuple[str, str]:
    message = str(exc)
    if "HTTP 401" in message:
        return "jira_auth", "Jira authentication failed"
    if "HTTP 403" in message:
        return "jira_permission", "Jira denied the requested read-only check"
    if "HTTP 404" in message:
        return "jira_metadata", "The configured Jira resource was not found"
    if "HTTP 429" in message:
        return "jira_rate_limit", "Jira rate-limited the readiness check"
    return "jira_connection", "Jira could not complete the read-only readiness check"


def _atomic_write_json(target: Path, value: dict[str, Any]) -> None:
    if not target.parent.exists():
        _fail(f"config parent directory does not exist: {target.parent}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _setup_discovery(base_url: str, project_key: str | None) -> dict[str, Any]:
    origin = _jira_origin(base_url)
    client = JiraClient(base_url=origin)
    identity = client.me()
    projects = [
        {"id": project.get("id"), "key": project.get("key"), "name": project.get("name")}
        for project in client.projects()
    ]
    result: dict[str, Any] = {
        "mode": "discovery",
        "writes_performed": False,
        "jira_origin": origin,
        "authenticated_user": identity.get("displayName") or identity.get("accountId"),
        "projects": projects,
    }
    if project_key:
        selected = client.project(project_key)
        issue_types = [
            {
                "id": issue_type.get("id"),
                "name": issue_type.get("name"),
                "subtask": bool(issue_type.get("subtask")),
            }
            for issue_type in client.issue_types(project_key)
        ]
        status_names = sorted(
            {
                str(status.get("name"))
                for group in client.project_statuses(project_key)
                for status in group.get("statuses", [])
                if status.get("name")
            },
            key=str.casefold,
        )
        result["selected_project"] = {
            "id": selected.get("id"),
            "key": selected.get("key"),
            "name": selected.get("name"),
        }
        result["issue_types"] = issue_types
        result["statuses"] = status_names
    return result


def command_setup(args: argparse.Namespace) -> dict[str, Any]:
    if args.project_key and not args.base_url:
        _fail("setup --project-key requires --base-url")
    if args.force and not args.write:
        _fail("setup --force requires --write")
    if args.base_url:
        if args.write or args.force or args.input:
            _fail("setup discovery cannot be combined with --input, --write, or --force")
        return _setup_discovery(args.base_url, args.project_key)

    target = Path(args.config).expanduser()
    example_path = Path(__file__).resolve().parents[1] / "config.example.json"
    proposal = _load_object(args.input) if args.input else _load_object(example_path)
    proposal.setdefault("version", CONFIG_VERSION)

    if args.input:
        validate_config(proposal)
        placeholders = _placeholder_errors(proposal)
        if placeholders:
            _fail("setup input is incomplete:\n- " + "\n- ".join(placeholders))
        secret_paths = _contains_secret_keys(proposal)
        if secret_paths:
            _fail("configuration must not contain secret-bearing keys: " + ", ".join(secret_paths))

    result: dict[str, Any] = {
        "dry_run": not args.write,
        "target": str(target),
        "config": _redact_account_ids(proposal),
        "credentials": ["JIRA_EMAIL", "JIRA_API_TOKEN"],
        "required_inputs": [
            "Jira Cloud base URL",
            "project key",
            "issue type",
            "ready, in-progress, in-review, and done statuses",
            "Git branch and pull-request conventions",
            "Pull-request base branch",
            "Jira-GitHub status sync mode",
        ],
        "jira_github_status_sync": (
            "automated mode requires GitHub for Atlassian, repository access, "
            "enabled PR-created and PR-merged automation rules, valid workflow "
            "transitions, Automation actor permissions, and protected human-approved merge"
        ),
    }
    if not args.write:
        return result
    if not args.input:
        _fail("setup --write requires an approved --input configuration")
    if target.exists() and not args.force:
        _fail(f"config already exists: {target}; review it or pass --force explicitly")
    _atomic_write_json(target, proposal)
    return {
        **result,
        "dry_run": False,
        "written": True,
        "next": "run check before Jira writes or repository edits",
    }


def _check_git(
    config: dict[str, Any] | None,
    *,
    repo: Path,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not shutil.which("git"):
        return [
            _check(
                "git_binary",
                "block",
                "git is not installed or not on PATH",
                required=True,
                remediation="Install Git and rerun check.",
            )
        ]

    code, root = _run_command(["git", "rev-parse", "--show-toplevel"], cwd=repo)
    if code != 0 or not root:
        return [
            _check(
                "git_repository",
                "block",
                "The target directory is not a Git repository",
                required=True,
                remediation="Run setup from the repository that contains the agents.",
            )
        ]
    repo_root = Path(root)
    checks.append(_check("git_repository", "pass", "Git repository detected", required=True))

    code, remote = _run_command(["git", "remote", "get-url", "origin"], cwd=repo_root)
    host = _remote_host(remote) if code == 0 else None
    if not host:
        checks.append(
            _check(
                "git_remote",
                "block",
                "No usable origin remote is configured",
                required=True,
                remediation="Configure the repository origin before implementation.",
            )
        )
    else:
        checks.append(_check("git_remote", "pass", f"Origin host detected: {host}", required=True))

    code, dirty = _run_command(["git", "status", "--porcelain"], cwd=repo_root)
    if code == 0 and dirty:
        checks.append(
            _check(
                "git_worktree",
                "warn",
                "The worktree contains existing changes that must be preserved",
                required=False,
            )
        )
    elif code == 0:
        checks.append(_check("git_worktree", "pass", "Worktree is clean", required=False))

    identity_missing = []
    for key in ("user.name", "user.email"):
        code, value = _run_command(["git", "config", key], cwd=repo_root)
        if code != 0 or not value:
            identity_missing.append(key)
    if identity_missing:
        checks.append(
            _check(
                "git_identity",
                "block",
                "Git identity is incomplete",
                required=True,
                remediation="Configure git user.name and user.email before committing.",
            )
        )
    else:
        checks.append(_check("git_identity", "pass", "Git identity is configured", required=True))

    pull_request = (config or {}).get("pull_request") or {}
    provider = pull_request.get("provider", "github")
    status_sync = pull_request.get("jira_status_sync", "automated")
    github_required = provider == "github" or status_sync == "automated"
    if github_required:
        if not host:
            pass
        elif host not in {"github.com", "www.github.com"}:
            checks.append(
                _check(
                    "github_remote",
                    "block",
                    "GitHub is required but origin is not github.com",
                    required=True,
                    remediation=(
                        "Use a GitHub origin, or set both pull_request.provider and "
                        "pull_request.jira_status_sync to manual."
                    ),
                )
            )
        elif not shutil.which("gh"):
            checks.append(
                _check(
                    "github_cli",
                    "block",
                    "GitHub CLI is not installed",
                    required=True,
                    remediation=(
                        "Install gh and run gh auth login, or explicitly configure "
                        "manual PR handoff and manual Jira status sync."
                    ),
                )
            )
        else:
            code, _ = _run_command(["gh", "auth", "status", "--hostname", "github.com"], cwd=repo_root)
            if code != 0:
                checks.append(
                    _check(
                        "github_auth",
                        "block",
                        "GitHub CLI is not authenticated for github.com",
                        required=True,
                        remediation="Run gh auth login and rerun check.",
                    )
                )
            else:
                checks.append(_check("github_auth", "pass", "GitHub CLI authentication works", required=True))
                code, repository_json = _run_command(
                    ["gh", "repo", "view", "--json", "nameWithOwner,viewerPermission"],
                    cwd=repo_root,
                )
                if code != 0:
                    checks.append(
                        _check(
                            "github_repository",
                            "block",
                            "GitHub CLI cannot read the origin repository",
                            required=True,
                            remediation="Verify repository access and the active gh account.",
                        )
                    )
                else:
                    try:
                        repository = json.loads(repository_json)
                    except json.JSONDecodeError:
                        repository = {}
                    permission = str(repository.get("viewerPermission") or "").upper()
                    permission_ok = (
                        permission in {"WRITE", "MAINTAIN", "ADMIN"}
                        if provider == "github"
                        else bool(permission)
                    )
                    if not permission_ok:
                        message = (
                            "GitHub CLI returned no verifiable repository permission"
                            if not permission
                            else "The active GitHub account cannot push to the origin repository"
                        )
                        checks.append(
                            _check(
                                "github_repository",
                                "block",
                                message,
                                required=True,
                                remediation="Use an account with write access or configure a fork/manual handoff.",
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                "github_repository",
                                "pass",
                                "GitHub repository is accessible",
                                required=True,
                            )
                        )
                        base_branch = str(
                            pull_request.get("base_branch") or ""
                        )
                        repository_name = str(
                            repository.get("nameWithOwner") or ""
                        )
                        branch_path = urllib.parse.quote(
                            base_branch, safe=""
                        )
                        code, _ = _run_command(
                            [
                                "gh",
                                "api",
                                "--method",
                                "GET",
                                f"repos/{repository_name}/branches/{branch_path}",
                            ],
                            cwd=repo_root,
                        )
                        checks.append(
                            _check(
                                "github_base_branch",
                                "pass" if code == 0 else "block",
                                (
                                    f"Configured pull-request base branch exists: "
                                    f"{base_branch}"
                                    if code == 0
                                    else (
                                        "Configured pull-request base branch was "
                                        f"not found: {base_branch}"
                                    )
                                ),
                                required=True,
                                remediation=(
                                    None
                                    if code == 0
                                    else (
                                        "Choose an existing target branch and "
                                        "re-approve the configuration."
                                    )
                                ),
                            )
                        )
    else:
        checks.append(
            _check(
                "pull_request_provider",
                "warn",
                "Pull requests use manual handoff",
                required=False,
            )
        )

    template_path = pull_request.get("template_path")
    if isinstance(template_path, str) and template_path:
        if (repo_root / template_path).exists():
            checks.append(_check("pull_request_template", "pass", "Pull-request template found", required=False))
        else:
            checks.append(
                _check(
                    "pull_request_template",
                    "warn",
                    "Configured pull-request template was not found; the bundled format will be used",
                    required=False,
                )
            )
    return checks


def command_check(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    config_path = Path(args.config).expanduser()
    config: dict[str, Any] | None = None
    jira_connection = str(getattr(args, "jira_connection", "rest"))
    if jira_connection not in VALID_JIRA_CONNECTIONS:
        _fail("jira_connection must be one of: mcp, rest")
    verified_external_checks = set(
        getattr(args, "verified_external_check", None) or []
    )
    invalid_external_checks = verified_external_checks - VALID_EXTERNAL_CHECKS
    if invalid_external_checks:
        _fail(
            "verified_external_check must be one of: "
            + ", ".join(sorted(VALID_EXTERNAL_CHECKS))
        )
    external_checks_required: list[str] = []

    checks.append(
        _check(
            "python_version",
            "pass" if sys.version_info >= (3, 10) else "block",
            f"Python {sys.version_info.major}.{sys.version_info.minor} detected",
            required=True,
            remediation="Install Python 3.10 or later." if sys.version_info < (3, 10) else None,
        )
    )

    if not config_path.exists():
        checks.append(
            _check(
                "config",
                "block",
                f"Configuration file is missing: {config_path}",
                required=True,
                remediation="Run jira-ticket-workflow setup first.",
            )
        )
    else:
        try:
            config = load_config(config_path)
            placeholders = _placeholder_errors(config)
            if placeholders:
                checks.append(
                    _check(
                        "config",
                        "block",
                        "; ".join(placeholders),
                        required=True,
                        remediation="Replace every example value through setup.",
                    )
                )
                config = None
            else:
                checks.append(_check("config", "pass", "Configuration is structurally valid", required=True))
        except ValueError:
            checks.append(
                _check(
                    "config",
                    "block",
                    "Configuration is invalid",
                    required=True,
                    remediation="Run setup preview and repair the reported fields.",
                )
            )

    if config and jira_connection == "mcp":
        if "jira_mcp" in verified_external_checks:
            checks.append(
                _check(
                    "jira_mcp",
                    "pass",
                    "The host reports that required Jira MCP checks passed",
                    required=True,
                )
            )
        else:
            checks.append(
                _check(
                    "jira_mcp",
                    "unverified",
                    "Jira connectivity is delegated to the host MCP and remains unverified",
                    required=True,
                    remediation=(
                        "Verify Jira identity, project access, search, issue types, "
                        "statuses, and write permissions with the host MCP, then rerun "
                        "check with --verified-external-check jira_mcp."
                    ),
                )
            )
            external_checks_required.append("jira_mcp")

    if config and jira_connection == "rest":
        configured_url = str(config["jira"].get("base_url") or "").rstrip("/")
        override_url = os.environ.get("JIRA_BASE_URL")
        site_mismatch = bool(
            override_url and _jira_origin(override_url) != _jira_origin(configured_url)
        )
        if site_mismatch:
            checks.append(
                _check(
                    "jira_url_override",
                    "block",
                    "JIRA_BASE_URL points to a different Jira site than the approved configuration",
                    required=True,
                    remediation="Unset JIRA_BASE_URL or update and re-approve the repository configuration.",
                )
            )

        missing_credentials = [
            name for name in ("JIRA_EMAIL", "JIRA_API_TOKEN") if not os.environ.get(name)
        ]
        if missing_credentials:
            checks.append(
                _check(
                    "jira_credentials",
                    "block",
                    "Required Jira environment variables are missing",
                    required=True,
                    remediation="Export JIRA_EMAIL and JIRA_API_TOKEN; never paste their values into chat.",
                )
            )
        elif site_mismatch:
            checks.append(
                _check(
                    "jira_online_checks",
                    "block",
                    "Jira online checks were skipped to avoid sending credentials to an unapproved site",
                    required=True,
                    remediation="Resolve the Jira site mismatch, then rerun check.",
                )
            )
        else:
            checks.append(_check("jira_credentials", "pass", "Jira credentials are present", required=True))
            try:
                client = JiraClient(config)
                identity = client.me()
                checks.append(
                    _check(
                        "jira_identity",
                        "pass",
                        f"Authenticated Jira user: {identity.get('displayName') or 'verified'}",
                        required=True,
                    )
                )

                project_key = config["jira"]["project_key"]
                project = client.project(project_key)
                checks.append(
                    _check(
                        "jira_project",
                        "pass",
                        f"Project {project.get('key') or project_key} is accessible",
                        required=True,
                    )
                )

                issue_types = client.issue_types(project_key)
                configured_type = config["jira"]["issue_type"]
                matched_issue_type = next(
                    (
                        item
                        for item in issue_types
                        if (
                            configured_type.get("id")
                            and str(item.get("id")) == str(configured_type.get("id"))
                        )
                        or (
                            configured_type.get("name")
                            and str(item.get("name") or "").casefold()
                            == str(configured_type.get("name")).casefold()
                        )
                    ),
                    None,
                )
                type_match = matched_issue_type is not None
                checks.append(
                    _check(
                        "jira_issue_type",
                        "pass" if type_match else "block",
                        "Configured issue type is available" if type_match else "Configured issue type is not available",
                        required=True,
                        remediation=None if type_match else "Run setup discovery and select an available issue type.",
                    )
                )

                available_statuses = {
                    str(status.get("name") or "").casefold()
                    for group in client.project_statuses(project_key)
                    for status in group.get("statuses", [])
                }
                missing_statuses = [
                    name
                    for name in config["jira"]["statuses"].values()
                    if str(name).casefold() not in available_statuses
                ]
                checks.append(
                    _check(
                        "jira_statuses",
                        "pass" if not missing_statuses else "block",
                        "Configured Jira statuses exist"
                        if not missing_statuses
                        else "Configured Jira statuses are missing: " + ", ".join(missing_statuses),
                        required=True,
                        remediation=None if not missing_statuses else "Run setup discovery and select valid status names.",
                    )
                )

                permission_keys = [
                    "BROWSE_PROJECTS",
                    "CREATE_ISSUES",
                    "TRANSITION_ISSUES",
                    "ADD_COMMENTS",
                ]
                if config["jira"].get("assign_to_current_user", True):
                    permission_keys.append("ASSIGN_ISSUES")
                permissions = client.permissions(project_key, permission_keys)
                missing_permissions = [
                    key
                    for key in permission_keys
                    if not bool((permissions.get(key) or {}).get("havePermission"))
                ]
                checks.append(
                    _check(
                        "jira_permissions",
                        "pass" if not missing_permissions else "block",
                        "Required Jira permissions are available"
                        if not missing_permissions
                        else "Missing Jira permissions: " + ", ".join(missing_permissions),
                        required=True,
                        remediation=None if not missing_permissions else "Ask a Jira administrator for the listed project permissions.",
                    )
                )

                component = config["jira"].get("component")
                if component:
                    components = client.components(project_key)
                    component_found = any(
                        str(item.get("name") or "").casefold() == str(component).casefold()
                        for item in components
                    )
                    checks.append(
                        _check(
                            "jira_component",
                            "pass" if component_found else "block",
                            "Configured component exists" if component_found else "Configured component does not exist",
                            required=True,
                            remediation=None if component_found else "Choose an existing component or remove it from config.",
                        )
                    )

                priority = config["jira"].get("default_priority")
                if priority:
                    priorities = client.priorities()
                    priority_found = any(
                        str(item.get("name") or "").casefold() == str(priority).casefold()
                        for item in priorities
                    )
                    checks.append(
                        _check(
                            "jira_priority",
                            "pass" if priority_found else "block",
                            "Configured priority exists" if priority_found else "Configured priority does not exist",
                            required=True,
                            remediation=None if priority_found else "Choose an available priority or remove the default.",
                        )
                    )

                cc = config["jira"].get("cc") or {}
                if cc.get("mode") == "field":
                    fields = client.fields()
                    configured_field = next(
                        (
                            item
                            for item in fields
                            if str(item.get("id")) == str(cc.get("field_id"))
                        ),
                        None,
                    )
                    schema = (configured_field or {}).get("schema") or {}
                    multi_user = (
                        schema.get("type") == "array" and schema.get("items") == "user"
                    ) or str(schema.get("custom") or "").endswith(":multiuserpicker")
                    createable = False
                    if matched_issue_type and matched_issue_type.get("id"):
                        create_fields = client.create_fields(
                            project_key, str(matched_issue_type["id"])
                        )
                        createable = any(
                            str(item.get("fieldId") or item.get("key"))
                            == str(cc.get("field_id"))
                            and "set" in (item.get("operations") or [])
                            for item in create_fields
                        )
                    field_found = bool(configured_field and multi_user and createable)
                    checks.append(
                        _check(
                            "jira_cc_field",
                            "pass" if field_found else "block",
                            "Configured CC field is a createable multi-user field"
                            if field_found
                            else "Configured CC field is not a createable multi-user field for this issue type",
                            required=True,
                            remediation=None
                            if field_found
                            else "Choose a multi-user field on the issue create screen, or use comment/none mode.",
                        )
                    )

                if cc.get("mode") in {"field", "comment"}:
                    configured_accounts = _unique_strings(cc.get("account_ids", []))
                    resolved_users = client.users(configured_accounts)
                    active_accounts = {
                        str(user.get("accountId"))
                        for user in resolved_users
                        if user.get("active", True) is not False
                    }
                    unresolved = [
                        account_id
                        for account_id in configured_accounts
                        if account_id not in active_accounts
                    ]
                    checks.append(
                        _check(
                            "jira_cc_accounts",
                            "pass" if not unresolved else "block",
                            "Configured CC accounts are active and accessible"
                            if not unresolved
                            else f"{len(unresolved)} configured CC account(s) could not be resolved as active",
                            required=True,
                            remediation=None
                            if not unresolved
                            else "Replace inactive or inaccessible CC account IDs through setup.",
                        )
                    )

                project = _jql_quote(project_key)
                client.search(
                    f'project = "{project}" ORDER BY updated DESC',
                    fields="summary",
                    limit=1,
                )
                checks.append(_check("jira_search", "pass", "Jira search access works", required=True))
                checks.append(
                    _check(
                        "jira_transition_paths",
                        "warn",
                        (
                            "Status names exist; exact transition paths are verified "
                            "externally for automated sync and against each real issue "
                            "for manual transitions"
                        ),
                        required=False,
                    )
                )
            except JiraError as exc:
                check_id, message = _jira_failure_message(exc)
                checks.append(
                    _check(
                        check_id,
                        "block",
                        message,
                        required=True,
                        remediation="Verify Jira credentials, site access, VPN, and project permissions.",
                    )
                )

    checks.extend(_check_git(config, repo=Path(args.repo).expanduser().resolve()))
    status_sync = (
        ((config or {}).get("pull_request") or {}).get(
            "jira_status_sync", "automated"
        )
    )
    if config and status_sync == "automated":
        external_check_details = {
            "jira_github_connection": (
                "GitHub for Atlassian, organization, and repository access",
                "Verify the app connection and repository access in references/github-integration.md.",
            ),
            "jira_automation_rules": (
                "enabled PR-created and PR-merged Jira Automation rules",
                "Verify each rule's trigger, scope, conditions, action, and enabled state.",
            ),
            "jira_workflow_automation": (
                "required workflow paths and Automation actor permissions",
                "Verify every configured transition path and the actor's Transition issues permission.",
            ),
            "github_merge_controls": (
                "protected human-approved merge controls on the configured base branch",
                "Verify the active ruleset or branch protection, approvals, bypass actors, and auto-merge policy.",
            ),
        }
        for check_id in AUTOMATED_STATUS_SYNC_CHECKS:
            label, remediation = external_check_details[check_id]
            if check_id in verified_external_checks:
                checks.append(
                    _check(
                        check_id,
                        "pass",
                        f"The host reports that {label} passed",
                        required=True,
                    )
                )
            else:
                checks.append(
                    _check(
                        check_id,
                        "unverified",
                        f"Automated status sync requires host verification of {label}",
                        required=True,
                        remediation=remediation,
                    )
                )
                external_checks_required.append(check_id)
    elif config:
        checks.append(
            _check(
                "jira_github_status_sync",
                "warn",
                "Jira status sync is manual; PR activity will not transition tickets automatically",
                required=False,
            )
        )
    blocked = any(check["state"] == "block" for check in checks)
    ready = not blocked and not external_checks_required
    if blocked:
        mode = "blocked"
    elif external_checks_required:
        mode = "external-verification-required"
    else:
        mode = "standard"
    return {
        "ready": ready,
        "mode": mode,
        "jira_connection": jira_connection,
        "external_checks_required": external_checks_required,
        "writes_performed": False,
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jira helper for the jira-ticket-workflow skill")
    parser.add_argument(
        "--config",
        default=(
            os.environ.get("JIRA_AGENT_WORKFLOW_CONFIG")
            or DEFAULT_CONFIG_NAME
        ),
        help=f"path to project configuration (default: {DEFAULT_CONFIG_NAME})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup")
    setup_mode = setup.add_mutually_exclusive_group()
    setup_mode.add_argument("--input", help="approved configuration JSON to preview or write")
    setup_mode.add_argument("--base-url", help="Jira Cloud URL for read-only setup discovery")
    setup.add_argument("--project-key", help="project to inspect during discovery")
    setup.add_argument("--write", action="store_true", help="write the approved input to --config")
    setup.add_argument("--force", action="store_true", help="replace an existing config explicitly")
    setup.set_defaults(handler=command_setup)

    check = subparsers.add_parser(
        "check", help="run read-only Jira, Git, and GitHub readiness checks"
    )
    check.add_argument("--repo", default=".", help="target repository for Git/GitHub checks")
    check.add_argument(
        "--jira-connection",
        choices=sorted(VALID_JIRA_CONNECTIONS),
        default="rest",
        help="verify Jira through REST or delegate Jira verification to the host MCP",
    )
    check.add_argument(
        "--verified-external-check",
        action="append",
        choices=sorted(VALID_EXTERNAL_CHECKS),
        default=[],
        help=(
            "record a required check already verified by the host; repeat for "
            "multiple checks and never use without evidence"
        ),
    )
    check.set_defaults(handler=command_check)

    validate = subparsers.add_parser("validate-config")
    validate.set_defaults(handler=command_validate_config)

    search = subparsers.add_parser("search")
    search.add_argument("--agent", required=True)
    search.add_argument("--terms", default="")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(handler=command_search)

    create = subparsers.add_parser("create")
    create.add_argument("--input", required=True)
    create.add_argument("--write", action="store_true", help="perform the Jira write")
    create.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    create.set_defaults(handler=command_create)

    comment = subparsers.add_parser("comment")
    comment.add_argument("issue")
    comment.add_argument("--input", required=True)
    comment.add_argument("--write", action="store_true", help="perform the Jira write")
    comment.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    comment.set_defaults(handler=command_comment)

    status = subparsers.add_parser("status")
    status.add_argument("issue")
    status.set_defaults(handler=command_status)

    transition = subparsers.add_parser("transition")
    transition.add_argument("issue")
    transition.add_argument("--to", required=True)
    transition.add_argument("--write", action="store_true", help="perform the Jira write")
    transition.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    transition.set_defaults(handler=command_transition)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
    except JiraError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "check" and not result.get("ready"):
        return 1
    if result.get("partial_success"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
