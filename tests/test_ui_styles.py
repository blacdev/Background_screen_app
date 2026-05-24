from __future__ import annotations

from ui_styles import DIALOG_THEME_STYLESHEET, apply_dialog_theme


class _FakeWidget:
    def __init__(self) -> None:
        self.stylesheet = ""

    def setStyleSheet(self, stylesheet: str) -> None:
        self.stylesheet = stylesheet


def test_apply_dialog_theme_sets_shared_base_stylesheet() -> None:
    widget = _FakeWidget()

    apply_dialog_theme(widget)

    assert widget.stylesheet == DIALOG_THEME_STYLESHEET
    assert "QDialog" in widget.stylesheet
    assert "QLabel#sectionTitle" in widget.stylesheet


def test_apply_dialog_theme_appends_extra_stylesheet() -> None:
    widget = _FakeWidget()

    apply_dialog_theme(widget, "QLabel#custom { color: red; }")

    assert widget.stylesheet.startswith(DIALOG_THEME_STYLESHEET)
    assert "QLabel#custom { color: red; }" in widget.stylesheet
