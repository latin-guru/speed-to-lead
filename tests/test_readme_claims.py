"""The README's "What it does" table links every feature to the tests that prove it.

This keeps that table honest: every row needs at least one proof link, and every linked
test file must exist (and be a test module), so the README cannot quietly drift.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
LINK = re.compile(r"\]\(([^)\s]+)\)")


def _feature_rows() -> list[tuple[str, str]]:
    text = README.read_text(encoding="utf-8")
    section = text.split("## What it does", 1)[1].split("\n## ", 1)[0]
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[:2] == ["Feature", "Proof"]:
            continue
        assert len(cells) == 2, f"malformed row: {line}"
        rows.append((cells[0], cells[1]))
    return rows


def test_the_table_is_there() -> None:
    assert len(_feature_rows()) >= 20


def test_every_feature_links_to_existing_test_files() -> None:
    for feature, proof in _feature_rows():
        paths = LINK.findall(proof)
        assert paths, f"no proof linked for: {feature}"
        for rel in paths:
            assert rel.startswith("tests/"), f"{feature}: {rel} is not a test path"
            path = ROOT / rel
            assert path.is_file(), f"{feature}: {rel} does not exist"
            assert path.name.startswith("test_"), f"{feature}: {rel} is not a test module"
            assert path.suffix == ".py"
            assert re.search(r"^def test_", path.read_text(encoding="utf-8"), re.MULTILINE), rel


def test_link_text_names_the_linked_file() -> None:
    for feature, proof in _feature_rows():
        for name, rel in re.findall(r"\[`([^`]+)`\]\(([^)]+)\)", proof):
            assert Path(rel).name == name, f"{feature}: [{name}] points at {rel}"
