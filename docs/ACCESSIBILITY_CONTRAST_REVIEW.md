# Background Screen Controller - Accessibility & Contrast Review

## Scope

This review focuses on practical accessibility improvements for the current desktop controller workflow:

- text/background contrast in core control surfaces
- keyboard reachability for key workflows
- status messaging clarity
- readability under typical office lighting and mixed-display environments

## Current findings

### 1) Contrast

- Primary body text is already dark on light surfaces and generally readable.
- Muted/supporting text was using lower-opacity dark text in multiple dialog contexts.
- Footer/status secondary text can be easy to miss at smaller UI scales.

### 2) Keyboard and focus

- Core dialogs rely on standard Qt controls (`QLineEdit`, `QComboBox`, `QListWidget`, `QPushButton`) and are keyboard navigable.
- Focus visibility exists but should remain a regression check in future style changes.

### 3) Operational messaging

- Status and warning copy is generally clear; LAN/firewall and diagnostics statuses are visible.
- Pending/unpaired LAN client status is now surfaced in network summary text and tooltips.

## Implemented improvements in this phase

- Increased contrast for shared muted dialog text (`sectionDescription` / `mutedText`) in `ui_styles.py`.
- Added this review as an operator-visible reference in Help.

## Validation checklist used

- Open key dialogs and verify readability at 100% and 125% scaling:
  - Manage Screens
  - Configured Screens
  - Screen Groups
  - Help Center
- Verify keyboard-only operation for common actions:
  - tab/shift-tab traversal
  - space/enter activation on controls
- Confirm warning/status labels remain legible when offline/error states are active.

## Follow-up recommendations

- Add a high-contrast theme toggle for low-vision operators.
- Add explicit WCAG contrast checks to style review before release.
- Include keyboard-only smoke checks in release checklist sign-off.
