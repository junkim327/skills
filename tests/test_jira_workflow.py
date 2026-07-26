from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "jira-ticket-workflow"
    / "scripts"
    / "jira_workflow.py"
)
SPEC = importlib.util.spec_from_file_location("public_jira_agent_workflow", MODULE_PATH)
assert SPEC and SPEC.loader
jira_workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jira_workflow)
STATUS_SYNC_EVENT_CHECKS = list(
    jira_workflow.AUTOMATED_STATUS_SYNC_EVENT_CHECKS
)


def config(*, cc_mode: str = "none") -> dict:
    field_id = "customfield_12345" if cc_mode == "field" else None
    return {
        "version": 2,
        "jira": {
            "base_url": "https://example.atlassian.net",
            "project_key": "ENG",
            "issue_type": {"name": "Task"},
            "component": "Platform",
            "default_priority": "Medium",
            "assign_to_current_user": True,
            "statuses": {
                "ready": "To Do",
                "in_progress": "In Progress",
                "in_review": "In Review",
                "done": "Done",
            },
            "cc": {
                "mode": cc_mode,
                "field_id": field_id,
                "comment_text": "CC",
                "account_ids": (
                    ["account-a", "account-b", "account-a"]
                    if cc_mode != "none"
                    else []
                ),
            },
        },
        "ticket": {
            "summary_templates": {
                "fix": "[Agent fix] {agent}: {title}",
                "new": "[New agent] {agent}: {title}",
            },
            "labels": ["agent-development"],
            "add_change_label": True,
            "add_agent_label": True,
            "extra_fields": {},
        },
        "git": {
            "fix_branch": "fix/{ticket}-{agent}-{slug}",
            "new_branch": "feat/{ticket}-{agent}",
            "commit": "{ticket} {change_type}({agent}): {summary}",
            "pull_request_title": "{ticket} {summary}",
        },
        "pull_request": {
            "provider": "github",
            "jira_status_sync": "automated",
            "base_branch": "main",
            "template_path": ".github/pull_request_template.md",
        },
    }


def ticket(*, change_type: str = "fix") -> dict:
    return {
        "change_type": change_type,
        "agent": "notification-agent" if change_type == "fix" else "audit-agent",
        "title": "Improve status classification",
        "discovery_depth": "light" if change_type == "fix" else "full",
        "background": "The team reviews agent output during operations.",
        "problem_or_need": "Expected delay and failure are not distinguishable.",
        "current_behavior": "Both states use the same label.",
        "expected_behavior": "Expected delay and failure use distinct labels.",
        "evidence": "Sanitized reproduction evidence.",
        "cause_or_need": "The classifier has no expected-delay state.",
        "scope": ["Status classification", "Regression tests"],
        "decisions_and_assumptions": ["Keep the output structure."],
        "non_goals": ["Redesign provider retries."],
        "acceptance_criteria": ["The two states are distinct."],
        "validation_plan": "Test both sides of the boundary.",
        "impact_and_risk": "Failure counts may change.",
        "rollback": "Restore the previous classifier.",
        "related_issues": ["ENG-100"],
        "references": ["docs/agent.md"],
        "priority": "High",
        "discovery_confirmed": True,
        "material_decisions_resolved": True,
        "duplicate_search_confirmed": True,
    }


class ConfigurationTest(unittest.TestCase):
    def test_example_shape_is_valid(self) -> None:
        example_path = MODULE_PATH.parents[1] / "config.example.json"
        with patch.dict(os.environ, {}, clear=True):
            validated = jira_workflow.validate_config(
                json.loads(example_path.read_text(encoding="utf-8"))
            )
        self.assertEqual(validated["jira"]["project_key"], "ENG")

    def test_field_cc_requires_field_id(self) -> None:
        value = config(cc_mode="field")
        value["jira"]["cc"]["field_id"] = None
        with self.assertRaisesRegex(ValueError, "field_id"):
            jira_workflow.validate_config(value)

    def test_extra_fields_cannot_override_core_fields(self) -> None:
        value = config()
        value["ticket"]["extra_fields"] = {"summary": "not allowed"}
        with self.assertRaisesRegex(ValueError, "cannot override"):
            jira_workflow.validate_config(value)

    def test_jira_url_must_use_trusted_cloud_host(self) -> None:
        value = config()
        value["jira"]["base_url"] = "https://jira.example.com"
        with self.assertRaisesRegex(ValueError, "atlassian.net"):
            jira_workflow.validate_config(value)

    def test_environment_override_does_not_hide_untrusted_config_url(self) -> None:
        value = config()
        value["jira"]["base_url"] = "https://jira.example.com"
        with (
            patch.dict(
                os.environ,
                {"JIRA_BASE_URL": "https://safe.atlassian.net"},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "atlassian.net"),
        ):
            jira_workflow.validate_config(value)

    def test_git_templates_require_ticket_key(self) -> None:
        value = config()
        value["git"]["pull_request_title"] = "{summary}"
        with self.assertRaisesRegex(ValueError, "ticket"):
            jira_workflow.validate_config(value)

    def test_unknown_keys_are_rejected_instead_of_defaulting(self) -> None:
        value = config()
        value["jira"]["assign_to_curent_user"] = False
        with self.assertRaisesRegex(ValueError, "unknown key: assign_to_curent_user"):
            jira_workflow.validate_config(value)

    def test_active_cc_mode_requires_accounts(self) -> None:
        value = config(cc_mode="comment")
        value["jira"]["cc"]["account_ids"] = []
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            jira_workflow.validate_config(value)

    def test_pull_request_template_cannot_escape_repository(self) -> None:
        value = config()
        value["pull_request"]["template_path"] = "../../private-file"
        with self.assertRaisesRegex(ValueError, "within the repository"):
            jira_workflow.validate_config(value)

    def test_in_review_status_is_required(self) -> None:
        value = config()
        del value["jira"]["statuses"]["in_review"]
        with self.assertRaisesRegex(ValueError, "in_review"):
            jira_workflow.validate_config(value)

    def test_jira_status_sync_rejects_ambiguous_optional_mode(self) -> None:
        value = config()
        value["pull_request"]["jira_status_sync"] = "optional"
        with self.assertRaisesRegex(ValueError, "automated, manual"):
            jira_workflow.validate_config(value)

    def test_pull_request_base_branch_must_be_safe(self) -> None:
        value = config()
        value["pull_request"]["base_branch"] = "bad..branch"
        with self.assertRaisesRegex(ValueError, "base_branch"):
            jira_workflow.validate_config(value)


class JiraClientMetadataTest(unittest.TestCase):
    def test_issue_types_uses_current_jira_response_key(self) -> None:
        client = jira_workflow.JiraClient.__new__(jira_workflow.JiraClient)
        with patch.object(
            client,
            "_call",
            return_value={"issueTypes": [{"id": "10001", "name": "Task"}]},
        ):
            issue_types = client.issue_types("ENG")
        self.assertEqual(issue_types[0]["name"], "Task")

    def test_config_backed_client_rejects_different_environment_site(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "JIRA_EMAIL": "user@example.com",
                    "JIRA_API_TOKEN": "token",
                    "JIRA_BASE_URL": "https://other.atlassian.net",
                },
                clear=True,
            ),
            self.assertRaisesRegex(jira_workflow.JiraError, "does not match"),
        ):
            jira_workflow.JiraClient(config())

    def test_jira_client_does_not_follow_redirects_with_authorization(self) -> None:
        with patch.dict(
            os.environ,
            {"JIRA_EMAIL": "user@example.com", "JIRA_API_TOKEN": "token"},
            clear=True,
        ):
            client = jira_workflow.JiraClient(config())

        redirect_handlers = [
            handler
            for handler in client.opener.handlers
            if isinstance(handler, jira_workflow.NoRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)


class CliTest(unittest.TestCase):
    def test_setup_preview_and_blocked_check_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preview = subprocess.run(
                [sys.executable, str(MODULE_PATH), "setup"],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
                env={},
            )
            check = subprocess.run(
                [sys.executable, str(MODULE_PATH), "check", "--repo", directory],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
                env={},
            )

        self.assertEqual(preview.returncode, 0)
        self.assertTrue(json.loads(preview.stdout)["dry_run"])
        self.assertEqual(check.returncode, 1)
        self.assertFalse(json.loads(check.stdout)["writes_performed"])


class TicketPlanTest(unittest.TestCase):
    def test_fix_persists_contract_not_internal_discovery(self) -> None:
        data = ticket()
        data.update(
            {
                "map_summary": "INTERNAL MAP",
                "territory_inspected": ["INTERNAL TERRITORY"],
                "known_unknowns": ["INTERNAL UNKNOWN"],
                "source_prompt": "RAW PROMPT",
            }
        )

        plan = jira_workflow.ticket_plan(config(), data)
        description = jira_workflow.adf_to_text(plan["fields"]["description"])

        self.assertEqual(
            plan["summary"],
            "[Agent fix] notification-agent: Improve status classification",
        )
        for heading in (
            "Background",
            "Problem",
            "Current behavior",
            "Expected behavior",
            "Evidence",
            "Cause",
            "Scope",
            "Acceptance criteria",
            "Validation plan",
            "Impact and risk",
            "Rollback",
        ):
            self.assertIn(heading, description)
        for internal in (
            "INTERNAL MAP",
            "INTERNAL TERRITORY",
            "INTERNAL UNKNOWN",
            "RAW PROMPT",
            "Unknown Matrix",
        ):
            self.assertNotIn(internal, description)

    def test_new_ticket_uses_new_capability_headings(self) -> None:
        data = ticket(change_type="new")
        data["decisions_and_assumptions"] = []
        data["non_goals"] = []
        data["related_issues"] = []
        data["references"] = []

        plan = jira_workflow.ticket_plan(config(), data)
        description = jira_workflow.adf_to_text(plan["fields"]["description"])

        self.assertIn("New capability", description)
        self.assertIn("Capability gap", description)
        self.assertNotIn("Decisions and assumptions", description)
        self.assertNotIn("Out of scope", description)
        self.assertNotIn("Related work and references", description)

    def test_empty_acceptance_criteria_is_rejected(self) -> None:
        data = ticket()
        data["acceptance_criteria"] = []
        with self.assertRaisesRegex(ValueError, "acceptance_criteria"):
            jira_workflow.ticket_plan(config(), data)

    def test_legacy_approved_field_is_rejected(self) -> None:
        data = ticket()
        data["approved"] = True
        with self.assertRaisesRegex(ValueError, "no longer supported"):
            jira_workflow.ticket_plan(config(), data)


class CcTest(unittest.TestCase):
    def test_field_cc_deduplicates_and_excludes_requester(self) -> None:
        plan = jira_workflow.ticket_plan(
            config(cc_mode="field"), ticket(), requester_id="account-a"
        )
        cc = plan["cc"]
        self.assertEqual(cc["account_ids"], ["account-b"])
        self.assertEqual(
            plan["fields"]["customfield_12345"], [{"accountId": "account-b"}]
        )
        self.assertEqual(plan["fields"]["assignee"], {"accountId": "account-a"})

    def test_comment_cc_builds_mentions_without_issue_field(self) -> None:
        plan = jira_workflow.ticket_plan(
            config(cc_mode="comment"), ticket(), requester_id="account-a"
        )
        self.assertNotIn("customfield_12345", plan["fields"])
        self.assertEqual(plan["cc"]["account_ids"], ["account-b"])
        mention_nodes = [
            node
            for paragraph in plan["cc"]["comment_adf"]["content"]
            for node in paragraph.get("content", [])
            if node.get("type") == "mention"
        ]
        self.assertEqual(
            mention_nodes,
            [{"type": "mention", "attrs": {"id": "account-b", "text": ""}}],
        )

    def test_comment_failure_reports_created_issue_without_retrying_create(self) -> None:
        class FakeClient:
            create_calls = 0

            def __init__(self, _config: dict):
                pass

            def me(self) -> dict:
                return {"accountId": "account-a"}

            def create_issue(self, _fields: dict) -> dict:
                self.__class__.create_calls += 1
                return {"key": "ENG-101", "id": "101", "url": "https://example/ENG-101"}

            def add_comment(self, _issue: str, _adf: dict) -> dict:
                raise jira_workflow.JiraError("comment permission denied")

        args = SimpleNamespace(
            config="unused", input="unused", dry_run=False, write=True
        )
        with (
            patch.object(jira_workflow, "load_config", return_value=config(cc_mode="comment")),
            patch.object(jira_workflow, "_load_object", return_value=ticket()),
            patch.object(jira_workflow, "JiraClient", FakeClient),
        ):
            result = jira_workflow.command_create(args)

        self.assertEqual(FakeClient.create_calls, 1)
        self.assertEqual(result["key"], "ENG-101")
        self.assertTrue(result["partial_success"])
        self.assertEqual(result["cc"]["comment"]["status"], "failed")
        self.assertIn("do not rerun create", result["cc"]["comment"]["warning"])


class WriteSafetyTest(unittest.TestCase):
    def test_create_defaults_to_redacted_preview(self) -> None:
        args = SimpleNamespace(
            config="unused", input="unused", dry_run=False, write=False
        )
        with (
            patch.object(
                jira_workflow, "load_config", return_value=config(cc_mode="field")
            ),
            patch.object(jira_workflow, "_load_object", return_value=ticket()),
            patch.object(jira_workflow, "JiraClient") as client,
        ):
            result = jira_workflow.command_create(args)

        client.assert_not_called()
        rendered = json.dumps(result)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["write_required"])
        self.assertNotIn("account-a", rendered)
        self.assertNotIn("account-b", rendered)
        self.assertIn("<redacted>", rendered)

    def test_comment_defaults_to_preview_and_hides_mentions(self) -> None:
        args = SimpleNamespace(
            config="unused",
            input="unused",
            issue="ENG-101",
            dry_run=False,
            write=False,
        )
        comment = {
            "text": "Progress update",
            "mention_account_ids": ["account-a", "account-b"],
        }
        with (
            patch.object(
                jira_workflow, "load_config", return_value=config(cc_mode="comment")
            ),
            patch.object(jira_workflow, "_load_object", return_value=comment),
            patch.object(jira_workflow, "JiraClient") as client,
        ):
            result = jira_workflow.command_comment(args)

        client.assert_not_called()
        rendered = json.dumps(result)
        self.assertEqual(result["mention_account_count"], 2)
        self.assertNotIn("account-a", rendered)
        self.assertNotIn("account-b", rendered)

    def test_comment_requires_write_flag_for_live_call(self) -> None:
        class FakeClient:
            comment_calls = 0

            def __init__(self, _config: dict):
                pass

            def add_comment(self, issue: str, _adf: dict) -> dict:
                self.__class__.comment_calls += 1
                return {"id": "10", "issue": issue}

        args = SimpleNamespace(
            config="unused",
            input="unused",
            issue="ENG-101",
            dry_run=False,
            write=True,
        )
        with (
            patch.object(jira_workflow, "load_config", return_value=config()),
            patch.object(
                jira_workflow, "_load_object", return_value={"text": "Approved update"}
            ),
            patch.object(jira_workflow, "JiraClient", FakeClient),
        ):
            result = jira_workflow.command_comment(args)

        self.assertEqual(FakeClient.comment_calls, 1)
        self.assertEqual(result["issue"], "ENG-101")

    def test_comment_rejects_issue_from_another_project(self) -> None:
        args = SimpleNamespace(
            config="unused",
            input="unused",
            issue="OPS-101",
            dry_run=False,
            write=False,
        )
        with (
            patch.object(jira_workflow, "load_config", return_value=config()),
            self.assertRaisesRegex(ValueError, "outside configured Jira project"),
        ):
            jira_workflow.command_comment(args)

    def test_transition_defaults_to_preview_and_limits_target_status(self) -> None:
        class FakeClient:
            transition_calls = 0

            def __init__(self, _config: dict):
                pass

            def transitions(self, _issue: str) -> list[dict]:
                return [
                    {"id": "1", "name": "Start", "to": {"name": "In Progress"}}
                ]

            def transition(self, _issue: str, _transition: str) -> None:
                self.__class__.transition_calls += 1

        args = SimpleNamespace(
            config="unused",
            issue="ENG-101",
            to="In Progress",
            dry_run=False,
            write=False,
        )
        with (
            patch.object(jira_workflow, "load_config", return_value=config()),
            patch.object(jira_workflow, "JiraClient", FakeClient),
        ):
            result = jira_workflow.command_transition(args)

        self.assertTrue(result["dry_run"])
        self.assertEqual(FakeClient.transition_calls, 0)

        args.write = True
        with (
            patch.object(jira_workflow, "load_config", return_value=config()),
            patch.object(jira_workflow, "JiraClient", FakeClient),
        ):
            result = jira_workflow.command_transition(args)
        self.assertTrue(result["transitioned"])
        self.assertEqual(FakeClient.transition_calls, 1)

        args.write = False
        args.to = "Canceled"
        with (
            patch.object(jira_workflow, "load_config", return_value=config()),
            self.assertRaisesRegex(ValueError, "configured Jira statuses"),
        ):
            jira_workflow.command_transition(args)


class TransitionTest(unittest.TestCase):
    def test_selects_transition_by_destination_case_insensitively(self) -> None:
        transitions = [
            {"id": "1", "name": "Start work", "to": {"name": "In Progress"}},
            {"id": "2", "name": "Close", "to": {"name": "Done"}},
        ]
        preview = jira_workflow._transition_preview(
            "ENG-101", "in progress", transitions
        )
        self.assertEqual(preview["transition_id"], "1")

    def test_in_review_is_an_allowed_configured_target(self) -> None:
        selected = jira_workflow._validated_target_status(config(), "in review")
        self.assertEqual(selected, "In Review")

    def test_rejects_unavailable_transition(self) -> None:
        with self.assertRaisesRegex(ValueError, "no transition"):
            jira_workflow._transition_preview(
                "ENG-101", "Done", [{"id": "1", "to": {"name": "To Do"}}]
            )


class SetupCommandTest(unittest.TestCase):
    def args(self, target: Path, **overrides: object) -> SimpleNamespace:
        values = {
            "config": str(target),
            "input": None,
            "base_url": None,
            "project_key": None,
            "write": False,
            "force": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_default_setup_is_preview_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".jira-ticket-workflow.json"
            result = jira_workflow.command_setup(self.args(target))
            self.assertTrue(result["dry_run"])
            self.assertFalse(target.exists())
            self.assertEqual(result["config"]["version"], 2)
            self.assertIn(
                "validates status synchronization after real pull-request and merge events",
                result["jira_github_status_sync"],
            )
            self.assertIn(
                "Git branch, worktree, and pull-request conventions",
                result["required_inputs"],
            )

    def test_setup_writes_approved_config_atomically_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "approved.json"
            target = root / ".jira-ticket-workflow.json"
            source.write_text(json.dumps(config()), encoding="utf-8")

            preview = jira_workflow.command_setup(
                self.args(target, input=str(source))
            )
            self.assertTrue(preview["dry_run"])
            self.assertFalse(target.exists())

            written = jira_workflow.command_setup(
                self.args(target, input=str(source), write=True)
            )
            self.assertTrue(written["written"])
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), config())
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

            original = target.read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                jira_workflow.command_setup(
                    self.args(target, input=str(source), write=True)
                )
            self.assertEqual(target.read_bytes(), original)

    def test_setup_rejects_placeholder_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "incomplete.json"
            target = root / ".jira-ticket-workflow.json"
            value = config()
            value["jira"]["base_url"] = "https://your-domain.atlassian.net"
            source.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "placeholder"):
                jira_workflow.command_setup(
                    self.args(target, input=str(source))
                )

    def test_setup_preview_redacts_account_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "proposal.json"
            target = root / ".jira-ticket-workflow.json"
            source.write_text(json.dumps(config(cc_mode="field")), encoding="utf-8")

            result = jira_workflow.command_setup(
                self.args(target, input=str(source))
            )

        rendered = json.dumps(result)
        self.assertNotIn("account-a", rendered)
        self.assertNotIn("account-b", rendered)
        self.assertIn("<redacted>", rendered)

    def test_setup_discovery_is_read_only(self) -> None:
        class FakeClient:
            def __init__(self, *, base_url: str):
                self.base_url = base_url

            def me(self) -> dict:
                return {"displayName": "Example User"}

            def projects(self) -> list[dict]:
                return [{"id": "1", "key": "ENG", "name": "Engineering"}]

            def project(self, key: str) -> dict:
                return {"id": "1", "key": key, "name": "Engineering"}

            def issue_types(self, _key: str) -> list[dict]:
                return [{"id": "10001", "name": "Task", "subtask": False}]

            def project_statuses(self, _key: str) -> list[dict]:
                return [{"statuses": [{"name": "To Do"}, {"name": "Done"}]}]

        args = self.args(
            Path(".jira-ticket-workflow.json"),
            base_url="https://example.atlassian.net",
            project_key="ENG",
        )
        with patch.object(jira_workflow, "JiraClient", FakeClient):
            result = jira_workflow.command_setup(args)
        self.assertFalse(result["writes_performed"])
        self.assertEqual(result["selected_project"]["key"], "ENG")
        self.assertEqual(result["issue_types"][0]["name"], "Task")


class CheckCommandTest(unittest.TestCase):
    def test_mcp_core_access_allows_deferred_operation_and_event_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            args = SimpleNamespace(
                config=str(path),
                repo=directory,
                jira_connection="mcp",
                verified_external_check=["jira_mcp"],
            )
            git_pass = [jira_workflow._check("git", "pass", "ok", required=True)]
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(jira_workflow, "_check_git", return_value=git_pass),
                patch.object(jira_workflow, "JiraClient") as client,
            ):
                result = jira_workflow.command_check(args)

            client.assert_not_called()

        self.assertTrue(result["ready"])
        self.assertEqual(result["jira_connection"], "mcp")
        self.assertEqual(result["mode"], "ready-with-deferred-checks")
        self.assertEqual(result["external_checks_required"], [])
        self.assertEqual(
            result["deferred_checks"],
            ["jira_operation_configuration", *STATUS_SYNC_EVENT_CHECKS],
        )

    def test_automated_status_sync_is_deferred_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            args = SimpleNamespace(
                config=str(path),
                repo=directory,
                jira_connection="mcp",
                verified_external_check=["jira_mcp"],
            )
            with patch.object(jira_workflow, "_check_git", return_value=[]):
                result = jira_workflow.command_check(args)

        merged_event = next(
            item
            for item in result["checks"]
            if item["id"] == "jira_pr_merged_status_sync"
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["mode"], "ready-with-deferred-checks")
        self.assertEqual(merged_event["state"], "warn")
        self.assertFalse(merged_event["required"])
        self.assertEqual(result["external_checks_required"], [])
        self.assertIn("jira_pr_merged_status_sync", result["deferred_checks"])
        self.assertIn("actor permission", merged_event["remediation"])

    def test_unverified_mcp_blocks_while_status_sync_remains_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            args = SimpleNamespace(
                config=str(path),
                repo=directory,
                jira_connection="mcp",
            )
            with patch.object(jira_workflow, "_check_git", return_value=[]):
                result = jira_workflow.command_check(args)

        self.assertFalse(result["ready"])
        self.assertEqual(result["mode"], "external-verification-required")
        self.assertEqual(result["external_checks_required"], ["jira_mcp"])
        self.assertEqual(
            result["deferred_checks"],
            ["jira_operation_configuration", *STATUS_SYNC_EVENT_CHECKS],
        )

    def test_manual_status_sync_does_not_require_integration_attestation(self) -> None:
        value = config()
        value["pull_request"]["jira_status_sync"] = "manual"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            args = SimpleNamespace(
                config=str(path),
                repo=directory,
                jira_connection="mcp",
                verified_external_check=["jira_mcp"],
            )
            with patch.object(jira_workflow, "_check_git", return_value=[]):
                result = jira_workflow.command_check(args)

        status_sync = next(
            item
            for item in result["checks"]
            if item["id"] == "jira_github_status_sync"
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["mode"], "ready-with-deferred-checks")
        self.assertEqual(
            result["deferred_checks"], ["jira_operation_configuration"]
        )
        self.assertEqual(status_sync["state"], "warn")
        self.assertFalse(status_sync["required"])

    def test_missing_config_is_blocked_without_jira_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                config=str(Path(directory) / "missing.json"),
                repo=directory,
            )
            with patch.object(jira_workflow, "_check_git", return_value=[]):
                result = jira_workflow.command_check(args)
        self.assertFalse(result["ready"])
        self.assertFalse(result["writes_performed"])
        self.assertIn("config", [item["id"] for item in result["checks"]])

    def test_missing_credentials_blocks_online_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            args = SimpleNamespace(config=str(path), repo=directory)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(jira_workflow, "_check_git", return_value=[]),
                patch.object(jira_workflow, "JiraClient") as client,
            ):
                result = jira_workflow.command_check(args)
            client.assert_not_called()
        self.assertFalse(result["ready"])
        credential = next(item for item in result["checks"] if item["id"] == "jira_credentials")
        self.assertEqual(credential["state"], "block")

    def test_different_environment_site_blocks_without_contacting_jira(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            args = SimpleNamespace(config=str(path), repo=directory)
            with (
                patch.dict(
                    os.environ,
                    {
                        "JIRA_EMAIL": "user@example.com",
                        "JIRA_API_TOKEN": "token",
                        "JIRA_BASE_URL": "https://other.atlassian.net",
                    },
                    clear=True,
                ),
                patch.object(jira_workflow, "_check_git", return_value=[]),
                patch.object(jira_workflow, "JiraClient") as client,
            ):
                result = jira_workflow.command_check(args)
            client.assert_not_called()
        mismatch = next(
            item for item in result["checks"] if item["id"] == "jira_url_override"
        )
        self.assertEqual(mismatch["state"], "block")
        self.assertFalse(result["ready"])

    def test_operation_specific_configuration_is_deferred(self) -> None:
        class FakeClient:
            def __init__(self, _config: dict):
                pass

            def me(self) -> dict:
                return {"displayName": "Example User"}

            def project(self, key: str) -> dict:
                return {"key": key}

            def search(self, _jql: str, *, fields: str, limit: int) -> list[dict]:
                return []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config(cc_mode="field")), encoding="utf-8")
            args = SimpleNamespace(
                config=str(path),
                repo=directory,
                verified_external_check=[],
            )
            with (
                patch.dict(
                    os.environ,
                    {"JIRA_EMAIL": "user@example.com", "JIRA_API_TOKEN": "token"},
                    clear=True,
                ),
                patch.object(jira_workflow, "JiraClient", FakeClient),
                patch.object(jira_workflow, "_check_git", return_value=[]),
            ):
                result = jira_workflow.command_check(args)
        self.assertTrue(result["ready"])
        operation_configuration = next(
            item
            for item in result["checks"]
            if item["id"] == "jira_operation_configuration"
        )
        self.assertEqual(operation_configuration["state"], "warn")
        self.assertFalse(operation_configuration["required"])
        self.assertIn("jira_operation_configuration", result["deferred_checks"])
        self.assertFalse(
            any(item["id"] == "jira_cc_field" for item in result["checks"])
        )

    def test_successful_check_uses_read_only_jira_methods(self) -> None:
        class FakeClient:
            def __init__(self, _config: dict):
                self.calls: list[str] = []

            def me(self) -> dict:
                return {"displayName": "Example User"}

            def project(self, key: str) -> dict:
                return {"key": key}

            def issue_types(self, _key: str) -> list[dict]:
                return [{"id": "10001", "name": "Task"}]

            def project_statuses(self, _key: str) -> list[dict]:
                return [
                    {
                        "statuses": [
                            {"name": "To Do"},
                            {"name": "In Progress"},
                            {"name": "In Review"},
                            {"name": "Done"},
                        ]
                    }
                ]

            def permissions(self, _key: str, permissions: list[str]) -> dict:
                return {key: {"havePermission": True} for key in permissions}

            def components(self, _key: str) -> list[dict]:
                return [{"name": "Platform"}]

            def priorities(self) -> list[dict]:
                return [{"name": "Medium"}]

            def search(self, _jql: str, *, fields: str, limit: int) -> list[dict]:
                self.assert_read_args = (fields, limit)
                return []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".jira-ticket-workflow.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            args = SimpleNamespace(
                config=str(path),
                repo=directory,
                verified_external_check=[],
            )
            git_pass = [jira_workflow._check("git", "pass", "ok", required=True)]
            with (
                patch.dict(
                    os.environ,
                    {"JIRA_EMAIL": "user@example.com", "JIRA_API_TOKEN": "token"},
                    clear=True,
                ),
                patch.object(jira_workflow, "JiraClient", FakeClient),
                patch.object(jira_workflow, "_check_git", return_value=git_pass),
            ):
                result = jira_workflow.command_check(args)
        self.assertTrue(result["ready"])
        self.assertEqual(result["mode"], "ready-with-deferred-checks")
        self.assertFalse(result["writes_performed"])
        self.assertTrue(any(item["id"] == "jira_search" for item in result["checks"]))


class CheckGitTest(unittest.TestCase):
    def test_git_and_github_checks_are_read_only(self) -> None:
        commands: list[tuple[str, ...]] = []

        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}" if binary in {"git", "gh"} else None

        def fake_run(command: list[str], *, cwd: Path, timeout: int = 15) -> tuple[int, str]:
            del cwd, timeout
            commands.append(tuple(command))
            responses = {
                ("git", "rev-parse", "--show-toplevel"): (0, "/tmp/repo"),
                ("git", "remote", "get-url", "origin"): (0, "git@github.com:owner/repo.git"),
                ("git", "status", "--porcelain"): (0, ""),
                ("git", "config", "user.name"): (0, "Example User"),
                ("git", "config", "user.email"): (0, "user@example.com"),
                ("gh", "auth", "status", "--hostname", "github.com"): (0, ""),
                (
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner,viewerPermission",
                ): (0, '{"nameWithOwner":"owner/repo","viewerPermission":"WRITE"}'),
                (
                    "gh",
                    "api",
                    "--method",
                    "GET",
                    "repos/owner/repo/branches/main",
                ): (0, '{"name":"main"}'),
            }
            return responses[tuple(command)]

        with (
            patch.object(jira_workflow.shutil, "which", side_effect=fake_which),
            patch.object(jira_workflow, "_run_command", side_effect=fake_run),
        ):
            checks = jira_workflow._check_git(config(), repo=Path("/tmp/repo"))

        self.assertFalse(any(item["state"] == "block" for item in checks))
        forbidden = {"push", "commit", "checkout", "switch", "reset", "pr"}
        self.assertFalse(any(forbidden.intersection(command[1:]) for command in commands))

    def test_missing_configured_base_branch_blocks(self) -> None:
        def fake_run(command: list[str], *, cwd: Path, timeout: int = 15) -> tuple[int, str]:
            del cwd, timeout
            responses = {
                ("git", "rev-parse", "--show-toplevel"): (0, "/tmp/repo"),
                ("git", "remote", "get-url", "origin"): (0, "git@github.com:owner/repo.git"),
                ("git", "status", "--porcelain"): (0, ""),
                ("git", "config", "user.name"): (0, "Example User"),
                ("git", "config", "user.email"): (0, "user@example.com"),
                ("gh", "auth", "status", "--hostname", "github.com"): (0, ""),
                (
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner,viewerPermission",
                ): (0, '{"nameWithOwner":"owner/repo","viewerPermission":"WRITE"}'),
                (
                    "gh",
                    "api",
                    "--method",
                    "GET",
                    "repos/owner/repo/branches/main",
                ): (1, ""),
            }
            return responses[tuple(command)]

        with (
            patch.object(jira_workflow.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(jira_workflow, "_run_command", side_effect=fake_run),
        ):
            checks = jira_workflow._check_git(config(), repo=Path("/tmp/repo"))

        base_branch = next(
            item for item in checks if item["id"] == "github_base_branch"
        )
        self.assertEqual(base_branch["state"], "block")

    def test_unverifiable_github_permission_blocks(self) -> None:
        def fake_run(command: list[str], *, cwd: Path, timeout: int = 15) -> tuple[int, str]:
            del cwd, timeout
            responses = {
                ("git", "rev-parse", "--show-toplevel"): (0, "/tmp/repo"),
                ("git", "remote", "get-url", "origin"): (0, "git@github.com:owner/repo.git"),
                ("git", "status", "--porcelain"): (0, ""),
                ("git", "config", "user.name"): (0, "Example User"),
                ("git", "config", "user.email"): (0, "user@example.com"),
                ("gh", "auth", "status", "--hostname", "github.com"): (0, ""),
                ("gh", "repo", "view", "--json", "nameWithOwner,viewerPermission"): (0, "not-json"),
            }
            return responses[tuple(command)]

        with (
            patch.object(jira_workflow.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(jira_workflow, "_run_command", side_effect=fake_run),
        ):
            checks = jira_workflow._check_git(config(), repo=Path("/tmp/repo"))

        github = next(item for item in checks if item["id"] == "github_repository")
        self.assertEqual(github["state"], "block")

    def test_manual_pr_provider_does_not_require_gh(self) -> None:
        value = config()
        value["pull_request"]["provider"] = "manual"
        value["pull_request"]["jira_status_sync"] = "manual"

        def fake_run(command: list[str], *, cwd: Path, timeout: int = 15) -> tuple[int, str]:
            del cwd, timeout
            responses = {
                ("git", "rev-parse", "--show-toplevel"): (0, "/tmp/repo"),
                ("git", "remote", "get-url", "origin"): (0, "git@example.com:owner/repo.git"),
                ("git", "status", "--porcelain"): (0, ""),
                ("git", "config", "user.name"): (0, "Example User"),
                ("git", "config", "user.email"): (0, "user@example.com"),
            }
            return responses[tuple(command)]

        with (
            patch.object(jira_workflow.shutil, "which", side_effect=lambda name: "/usr/bin/git" if name == "git" else None),
            patch.object(jira_workflow, "_run_command", side_effect=fake_run),
        ):
            checks = jira_workflow._check_git(value, repo=Path("/tmp/repo"))

        self.assertFalse(any(item["id"].startswith("github_") for item in checks))
        provider = next(item for item in checks if item["id"] == "pull_request_provider")
        self.assertEqual(provider["state"], "warn")


class DocumentationContractTest(unittest.TestCase):
    def test_task_worktree_is_the_default_implementation_isolation(self) -> None:
        skill = MODULE_PATH.parents[1].joinpath("SKILL.md").read_text(
            encoding="utf-8"
        )
        configuration = MODULE_PATH.parents[1].joinpath(
            "references", "configuration.md"
        ).read_text(encoding="utf-8")
        normalized_skill = " ".join(skill.split())
        normalized_configuration = " ".join(configuration.split())

        self.assertIn(
            "Default to a task-dedicated linked worktree",
            normalized_skill,
        )
        self.assertIn(
            "git worktree add -b <task-branch> <worktree-path> <approved-base-ref>",
            normalized_configuration,
        )
        self.assertIn(
            "Run all implementation commands with the task worktree",
            normalized_configuration,
        )
        self.assertIn(
            "obtain explicit approval before any forced removal",
            normalized_configuration,
        )

    def test_automated_sync_guide_defers_checks_until_real_events(self) -> None:
        guide = (
            MODULE_PATH.parents[1] / "references" / "github-integration.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(guide.split())

        self.assertIn(
            "Do not block implementation because the host cannot inspect",
            normalized,
        )
        self.assertIn(
            "The missing automatic transition does not invalidate completed code",
            normalized,
        )
        self.assertIn(
            "Repository review and merge policies are owned separately",
            normalized,
        )
        self.assertIn(
            "Do not turn missing admin access into a repository implementation blocker",
            normalized,
        )
        self.assertIn(
            "actual base branch matches `pull_request.base_branch`",
            normalized,
        )


if __name__ == "__main__":
    unittest.main()
