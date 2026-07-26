from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "jira-ticket-workflow"


class PackageLayoutTest(unittest.TestCase):
    def test_repository_files_use_publishable_layout(self) -> None:
        expected_paths = (
            REPOSITORY_ROOT / "README.md",
            REPOSITORY_ROOT / "LICENSE",
            REPOSITORY_ROOT / ".github" / "workflows" / "validate.yml",
            REPOSITORY_ROOT / "tests" / "test_jira_workflow.py",
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "config.example.json",
            SKILL_ROOT / "agents" / "openai.yaml",
            SKILL_ROOT / "references" / "configuration.md",
            SKILL_ROOT / "references" / "content-format.md",
            SKILL_ROOT / "references" / "github-integration.md",
            SKILL_ROOT / "scripts" / "jira_workflow.py",
        )

        missing_paths = [
            str(path.relative_to(REPOSITORY_ROOT))
            for path in expected_paths
            if not path.is_file()
        ]

        self.assertEqual(missing_paths, [])
if __name__ == "__main__":
    unittest.main()
