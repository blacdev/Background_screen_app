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


def test_user_guide_style_content_can_be_parsed_from_inline_markdown() -> None:
    text = """
# Background Screen Controller
## Start here - complete setup from start to finish
1. Engine > Start Engine
2. Library > Set Media Folder
3. Screen > Manage Screens
4. Build your first schedule
5. Start playback.
""".strip()
    sections = dict(main_module.markdown_sections(text))

    assert "Start here - complete setup from start to finish" in sections
    start_here = sections["Start here - complete setup from start to finish"]
    assert "Engine > Start Engine" in start_here
    assert "Library > Set Media Folder" in start_here
    assert "Screen > Manage Screens" in start_here
    assert "Build your first schedule" in start_here
    assert "Start playback." in start_here


def test_release_checklist_style_content_contains_build_and_validation_gates() -> None:
    text = """
# Release Checklist
## 2) Build artifacts
- build_exe.bat
- build_installer.bat
## 5) Diagnostics and operations
- Export Diagnostics
## 7) Final release gate
- Run targeted automated tests
""".strip()

    assert "## 2) Build artifacts" in text
    assert "build_exe.bat" in text
    assert "build_installer.bat" in text
    assert "## 5) Diagnostics and operations" in text
    assert "Export Diagnostics" in text
    assert "## 7) Final release gate" in text


def test_accessibility_review_style_content_contains_scope_and_follow_up_actions() -> (
    None
):
    text = """
# Accessibility Review
## Scope
## Current findings
## Implemented improvements in this phase
## Follow-up recommendations
""".strip()

    assert "## Scope" in text
    assert "## Current findings" in text
    assert "## Implemented improvements in this phase" in text
    assert "## Follow-up recommendations" in text
