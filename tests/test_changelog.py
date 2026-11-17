import re
from pathlib import Path

from roosta import app


def test_version_is_first_release_heading():
    text = "# Changelog\n\nIntro `## 9.9.9` inline.\n\n## 1.4.2 - 2026-11-08\n- x\n\n## 1.4.1 - 2026-11-07\n"
    assert app.changelog_version(text) == "1.4.2"
    assert app.changelog_version("# Changelog\n") == "0.0.0"


def test_live_changelog_has_a_dated_version_and_newest_first():
    text = (Path(app.DASHBOARD_ROOT) / "CHANGELOG.md").read_text()
    releases = re.findall(r"^## (\d+)\.(\d+)\.(\d+) - \d{4}-\d{2}-\d{2}$", text, re.M)
    assert releases, "CHANGELOG.md needs at least one '## x.y.z - YYYY-MM-DD' heading"
    versions = [tuple(map(int, r)) for r in releases]
    assert versions == sorted(versions, reverse=True), "versions must be unique and newest first"
    assert len(set(versions)) == len(versions)


def test_package_version_matches_the_changelog():
    import roosta

    text = (Path(app.DASHBOARD_ROOT) / "CHANGELOG.md").read_text()
    top = re.search(r"^## (\d+\.\d+\.\d+)", text, re.M).group(1)
    assert roosta.__version__ == top
