"""The release workflow publishes what ``scripts/changelog.py`` extracts.

A wrong cut here would publish the neighbouring release's notes or the link
references, so the boundaries are tested on a small changelog and on the real
file, which must have a section for every release it links.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "changelog.py"

_spec = importlib.util.spec_from_file_location("changelog", SCRIPT)
assert _spec and _spec.loader
changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog)

SAMPLE = """# Changelog

## [Unreleased]

### Added

- Something new.

## [1.1.0] - 2026-01-02

### Fixed

- A bug.

## [1.0.0] - 2026-01-01

### Added

- The start.

[Unreleased]: https://example.org/compare/v1.1.0...HEAD
[1.1.0]: https://example.org/compare/v1.0.0...v1.1.0
[1.0.0]: https://example.org/releases/tag/v1.0.0
"""


def test_a_middle_section_stops_at_the_next_heading():
    assert changelog.section(SAMPLE, "1.1.0") == "### Fixed\n\n- A bug."


def test_the_last_section_stops_at_the_link_references():
    assert changelog.section(SAMPLE, "1.0.0") == "### Added\n\n- The start."


def test_a_missing_version_is_refused():
    with pytest.raises(LookupError):
        changelog.section(SAMPLE, "2.0.0")


def test_an_empty_section_is_refused():
    with pytest.raises(LookupError):
        changelog.section("## [1.0.0] - 2026-01-01\n\n## [0.9.0]\n- x\n", "1.0.0")


@pytest.mark.parametrize("tag", ["1.0.0", "v1.0", "v1.0.0-rc1", "release-1"])
def test_only_plain_semver_tags_are_release_tags(tag):
    with pytest.raises(ValueError):
        changelog.version_of(tag)


def test_main_prints_the_notes(capsys):
    assert changelog.main(["notes", "v0.1.0"]) == 0
    assert "### Added" in capsys.readouterr().out


def test_main_fails_for_a_tag_without_notes(capsys):
    assert changelog.main(["notes", "v99.0.0"]) == 1
    assert "99.0.0" in capsys.readouterr().err


def test_the_pyproject_version_has_a_release_section():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert changelog.section(text, changelog.project_version(pyproject))


def test_every_linked_release_has_notes():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    linked = re.findall(r"^\[(\d+\.\d+\.\d+)\]: ", text, flags=re.MULTILINE)
    assert linked
    for version in linked:
        assert changelog.section(text, version)
