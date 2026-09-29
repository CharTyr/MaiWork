"""服务群保存后，网页不能继续沿用旧的群列表。"""

from pathlib import Path


JS = Path(__file__).resolve().parents[1] / "maiwork" / "console" / "static" / "js"


def _between(file: str, start: str, end: str) -> str:
    text = (JS / file).read_text(encoding="utf-8")
    assert start in text and end in text
    return text.split(start, 1)[1].split(end, 1)[0]


def test_config_save_refreshes_groups_and_page() -> None:
    save = _between("main.js", 'if (f.id === "rules-form") {', 'if (f.classList.contains("rss-add")) {')
    assert "await loadGroups()" in save
    assert "applyHash()" in save
    assert "render()" in save


def test_onboarding_refreshes_groups_and_landing() -> None:
    save = _between("onboarding.js", "async function onbSave(id) {", "async function onbGo(delta) {")
    close = _between("onboarding.js", "async function closeOnboarding(action) {", "async function onbSave(id) {")
    assert "await loadGroups()" in save
    assert "render()" in close
