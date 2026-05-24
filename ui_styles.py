from __future__ import annotations

from typing import Any

DIALOG_THEME_STYLESHEET = """
QDialog {
    background: #f6fafd;
    color: #173043;
}
QLabel {
    color: #173043;
}
QLabel#sectionTitle {
    font-size: 16px;
    font-weight: 650;
    color: #112c40;
}
QLabel#sectionDescription,
QLabel#mutedText {
    color: rgba(23,48,67,0.90);
    font-size: 12px;
}
QLabel#manageScreensFieldLabel {
    color: #112c40;
    font-size: 13px;
    font-weight: 650;
}
QLabel#manageScreensDetail {
    color: rgba(23,48,67,0.90);
    font-size: 12px;
}
QLabel#messageBanner {
    background: rgba(93,183,240,0.10);
    border: 1px solid rgba(93,183,240,0.25);
    border-radius: 12px;
    padding: 10px 14px;
    color: #102b3d;
}
""".strip()


def apply_dialog_theme(widget: Any, extra_stylesheet: str = "") -> None:
    stylesheet = DIALOG_THEME_STYLESHEET
    if extra_stylesheet.strip():
        stylesheet = f"{stylesheet}\n{extra_stylesheet.strip()}"
    widget.setStyleSheet(stylesheet)
