"""Read release notes out of CHANGELOG.md for the release workflow.

The changelog follows Keep a Changelog: one ``## [X.Y.Z] - YYYY-MM-DD``
heading per release, the link references at the bottom. A GitHub release is
published with the body of its version's section, so the changelog stays the
only place release notes are written.

Usage::

    python scripts/changelog.py notes v0.6.1          # print the section body
    python scripts/changelog.py check-version v0.6.1  # tag == pyproject version

Both commands exit with 1 and a message on stderr when the check fails, so a
tag without a changelog entry, or one that disagrees with ``pyproject.toml``,
does not get a release.
"""

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

SEMVER_TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")
SECTION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")
LINK_REFERENCE = re.compile(r"^\[[^\]]+\]: ")


def version_of(tag: str) -> str:
    """``v1.2.3`` -> ``1.2.3``; anything else is refused."""
    match = SEMVER_TAG.match(tag)
    if not match:
        raise ValueError(f"{tag!r} is not a release tag of the form vX.Y.Z")
    return match.group(1)


def section(text: str, version: str) -> str:
    """Return the body of ``## [version]`` without its heading.

    The body ends at the next ``## `` heading or at the link references that
    close the file. Raises ``LookupError`` when the version has no section or
    the section is empty.
    """
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        heading = SECTION_HEADING.match(line)
        if heading:
            if inside:
                break
            inside = heading.group("version") == version
            continue
        if not inside:
            continue
        if line.startswith("## ") or LINK_REFERENCE.match(line):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise LookupError(f"CHANGELOG.md has no entries for {version}")
    return notes


def project_version(pyproject: str) -> str:
    return str(tomllib.loads(pyproject)["project"]["version"])


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("notes", "check-version"):
        sys.stderr.write(__doc__ or "")
        return 2
    command, tag = argv
    try:
        version = version_of(tag)
        if command == "notes":
            notes = section(CHANGELOG.read_text(encoding="utf-8"), version)
            sys.stdout.write(notes + "\n")
        else:
            declared = project_version(PYPROJECT.read_text(encoding="utf-8"))
            if declared != version:
                raise ValueError(
                    f"tag {tag} does not match version {declared} in pyproject.toml"
                )
    except (ValueError, LookupError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
