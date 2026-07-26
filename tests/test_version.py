"""The single version constant is well-formed and agrees with the CHANGELOG.

src/version.py is the one place a version literal belongs in code (the OpenFoodFacts
User-Agent in src/worker/off.py and the FastAPI/OpenAPI version in src/api/main.py both read
it), and the CHANGELOG is the release record. These two must not drift apart.

Deliberately no git-tag assertion: tagging happens after the release commit, so a tag check
would fail on every pre-release working tree.
"""

import re
from pathlib import Path

from src.version import __version__

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)

_CHANGELOG = Path(__file__).parents[1] / "CHANGELOG.md"


def _top_released_changelog_version() -> str:
    """The version of the newest released CHANGELOG heading.

    Headings run newest-first, so the first one whose bracket contents parse as SemVer is the
    current release. `## [Unreleased]` is skipped by that test rather than by name, and the
    trailing ` - <date>` is ignored entirely — only the version portion is compared.
    """
    for match in _HEADING.finditer(_CHANGELOG.read_text(encoding="utf-8")):
        if _SEMVER.match(match.group(1)):
            return match.group(1)
    raise AssertionError("No released '## [x.y.z]' heading found in CHANGELOG.md")


def test_version_is_wellformed_semver():
    assert _SEMVER.match(__version__), __version__


def test_version_matches_top_released_changelog_heading():
    assert __version__ == _top_released_changelog_version()
