from __future__ import annotations

import main as main_module


def test_markdown_sections_extract_help_topics() -> None:
    markdown = """
# Title
## Start here - complete setup from start to finish
Line one
Line two
## Add local HDMI screens
Details here
""".strip()

    sections = main_module.markdown_sections(markdown)

    assert sections == [
        ("Start here - complete setup from start to finish", "Line one\nLine two"),
        ("Add local HDMI screens", "Details here"),
    ]


def test_markdown_to_help_html_renders_lists_and_subheadings() -> None:
    html = main_module.markdown_to_help_html(
        """
### Steps
1. Launch the app
2. Start the engine
- Confirm screens
""".strip()
    )

    assert "<h3" in html
    assert "Launch the app" in html
    assert "<ol" in html
    assert "<ul" in html


def test_user_guide_contains_complete_start_to_finish_setup_flow() -> None:
    guide_path = main_module.APP_ROOT / "docs" / "USER_GUIDE.md"
    text = guide_path.read_text(encoding="utf-8")
    sections = dict(main_module.markdown_sections(text))

    assert "Start here - complete setup from start to finish" in sections
    start_here = sections["Start here - complete setup from start to finish"]
    assert "Engine > Start Engine" in start_here
    assert "Library > Set Media Folder" in start_here
    assert "Screen > Manage Screens" in start_here
    assert "Build your first schedule" in start_here
    assert "Start playback." in start_here


def test_release_checklist_contains_build_and_validation_gates() -> None:
    checklist_path = main_module.APP_ROOT / "docs" / "RELEASE_CHECKLIST.md"
    text = checklist_path.read_text(encoding="utf-8")

    assert "## 2) Build artifacts" in text
    assert "build_exe.bat" in text
    assert "build_installer.bat" in text
    assert "## 5) Diagnostics and operations" in text
    assert "Export Diagnostics" in text
    assert "## 7) Final release gate" in text


def test_accessibility_review_contains_scope_and_follow_up_actions() -> None:
    review_path = main_module.APP_ROOT / "docs" / "ACCESSIBILITY_CONTRAST_REVIEW.md"
    text = review_path.read_text(encoding="utf-8")

    assert "## Scope" in text
    assert "## Current findings" in text
    assert "## Implemented improvements in this phase" in text
    assert "## Follow-up recommendations" in text
