#!/usr/bin/env python3
"""Validate Movies Nerd skill structure and release metadata."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
LOCAL_LINK = re.compile(r"\[[^\]]+\]\((?!https?://|#)([^)]+)\)")
SCRIPT_REFERENCE = re.compile(r"`(scripts/[^` ]+)`")


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    try:
        raw, _body = text[4:].split("\n---\n", 1)
    except ValueError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    version_file = root / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    if not SEMVER.fullmatch(version):
        errors.append("VERSION must contain one semantic version")

    skill_file = root / "SKILL.md"
    skill = skill_file.read_text(encoding="utf-8") if skill_file.is_file() else ""
    frontmatter = _frontmatter(skill)
    if set(frontmatter) != {"name", "description"}:
        errors.append("SKILL.md frontmatter must contain only name and description")
    if frontmatter.get("name") != "movies-nerd":
        errors.append("SKILL.md name must be movies-nerd")
    description = frontmatter.get("description", "")
    if not description or len(description) > 1024:
        errors.append("SKILL.md description must contain 1-1024 characters")

    workflow = skill.partition("## Workflow\n")[2].partition("\n## Entry points")[0]
    public_scripts = set(SCRIPT_REFERENCE.findall(workflow))
    if public_scripts - {"scripts/movies-nerd"}:
        errors.append("the public workflow must invoke only scripts/movies-nerd")

    for document in (root / "SKILL.md", root / "README.md"):
        if not document.is_file():
            errors.append(f"missing {document.name}")
            continue
        text = document.read_text(encoding="utf-8")
        for target in LOCAL_LINK.findall(text):
            clean_target = target.split("#", 1)[0]
            if clean_target and not (document.parent / clean_target).exists():
                errors.append(f"broken local link in {document.name}: {target}")

    readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").is_file() else ""
    if version and f"Version: **{version}**" not in readme:
        errors.append("README.md version does not match VERSION")

    agent = root / "agents" / "openai.yaml"
    agent_text = agent.read_text(encoding="utf-8") if agent.is_file() else ""
    if "display_name:" not in agent_text or "$movies-nerd" not in agent_text:
        errors.append("agents/openai.yaml is missing its display name or default skill prompt")

    launcher = root / "scripts" / "movies-nerd"
    if not launcher.is_file() or launcher.stat().st_mode & 0o111 == 0:
        errors.append("scripts/movies-nerd must exist and be executable")
    if not (root / "tests" / "helpers.py").is_file():
        errors.append("tests/helpers.py is required for shared fixtures")
    for test_file in sorted((root / "tests").glob("test_*.py")):
        if len(test_file.read_text(encoding="utf-8").splitlines()) > 700:
            errors.append(f"domain test file is too large: {test_file.name}")

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print("Movies Nerd project validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
