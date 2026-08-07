from pathlib import Path

# Derived from this file, not from the process's working directory: a relative path made these
# tests pass only when pytest happened to be invoked from the repository root, and fail with a
# `FileNotFoundError` that says nothing about frontmatter anywhere else.
SKILL = Path(__file__).resolve().parents[1] / "skills" / "generating-h3-video" / "SKILL.md"


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    block = text.split("---\n", 2)[1]
    return dict(
        (k.strip(), v.strip())
        for k, v in (line.split(":", 1) for line in block.splitlines() if ":" in line)
    )


def test_frontmatter_has_the_two_required_fields():
    fm = _frontmatter(SKILL.read_text())
    assert set(fm) >= {"name", "description"}
    assert len(SKILL.read_text().split("---\n")[1]) <= 1024


def test_name_is_runtime_portable():
    """Codex and Claude Code both key on the directory name; keep it lowercase-hyphen."""
    fm = _frontmatter(SKILL.read_text())
    assert fm["name"].replace("-", "").isalnum() and fm["name"].islower()


def test_description_states_triggers_not_workflow():
    fm = _frontmatter(SKILL.read_text())
    assert fm["description"].startswith("Use when")
    for leak in ("first", "then", "step 1"):
        assert leak not in fm["description"].lower(), (
            "a description that summarises the workflow gets followed instead of the skill body"
        )
