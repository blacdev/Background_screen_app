from __future__ import annotations

import pytest

from screen_groups import (
    DEFAULT_GROUP_ID,
    DEFAULT_GROUP_NAME,
    ScreenGroup,
    combined_screen_groups,
    default_screen_group,
    expand_target_ids,
    is_group_target_id,
    normalize_schedule_target_ids,
    parse_screen_groups,
    target_contains_screen,
    target_label_map,
    validate_screen_groups,
)


def test_is_group_target_id_detects_group_prefix() -> None:
    assert is_group_target_id("group:main") is True
    assert is_group_target_id("screen-1") is False


def test_screen_group_validation_normalizes_members() -> None:
    group = ScreenGroup(id="group:team", name="Team", screen_ids=["screen-1", "screen-1", " ", "group:oops", "screen-2"])
    group.validate()
    assert group.screen_ids == ["screen-1", "screen-2"]


def test_screen_group_to_dict_round_trips() -> None:
    group = ScreenGroup.from_dict({"id": "group:team", "name": "Team", "screen_ids": ["screen-1"]})
    assert group.to_dict() == {"id": "group:team", "name": "Team", "screen_ids": ["screen-1"]}


def test_screen_group_requires_group_prefix() -> None:
    with pytest.raises(ValueError, match="must start with group:"):
        ScreenGroup.from_dict({"id": "screen-1", "name": "Team", "screen_ids": []})


def test_screen_group_requires_name() -> None:
    with pytest.raises(ValueError, match="must have a name"):
        ScreenGroup.from_dict({"id": "group:team", "name": "", "screen_ids": []})


def test_screen_group_from_dict_generates_id_when_missing() -> None:
    group = ScreenGroup.from_dict({"name": "Team", "screen_ids": []})
    assert group.id.startswith("group:")


def test_default_screen_group_uses_expected_id_and_name() -> None:
    group = default_screen_group(["screen-1", "screen-2"])
    assert group.id == DEFAULT_GROUP_ID
    assert group.name == DEFAULT_GROUP_NAME
    assert group.screen_ids == ["screen-1", "screen-2"]


def test_parse_screen_groups_skips_invalid_duplicates_and_default_group() -> None:
    groups = parse_screen_groups(
        [
            {"id": "group:team", "name": "Team", "screen_ids": ["screen-1"]},
            {"id": "group:team", "name": "Dup", "screen_ids": ["screen-2"]},
            {"id": DEFAULT_GROUP_ID, "name": "Default", "screen_ids": ["screen-3"]},
            "bad-entry",
            {"id": "bad", "name": "Bad", "screen_ids": []},
        ]
    )
    assert [group.id for group in groups] == ["group:team"]


def test_parse_screen_groups_returns_empty_for_non_lists() -> None:
    assert parse_screen_groups(None) == []


def test_validate_screen_groups_rejects_non_list_payloads() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        validate_screen_groups({"id": "group:team"})


def test_validate_screen_groups_rejects_non_objects() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        validate_screen_groups(["bad"])


def test_validate_screen_groups_rejects_default_group_override() -> None:
    with pytest.raises(ValueError, match="managed by the app"):
        validate_screen_groups([{"id": DEFAULT_GROUP_ID, "name": "Default", "screen_ids": []}])


def test_validate_screen_groups_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        validate_screen_groups(
            [
                {"id": "group:team", "name": "Team", "screen_ids": []},
                {"id": "group:team", "name": "Other", "screen_ids": []},
            ]
        )


def test_validate_screen_groups_accepts_empty_lists() -> None:
    assert validate_screen_groups([]) == []


def test_combined_screen_groups_includes_default_then_extras() -> None:
    extra = [ScreenGroup.from_dict({"id": "group:team", "name": "Team", "screen_ids": ["screen-2"]})]
    groups = combined_screen_groups(["screen-1"], extra)
    assert [group.id for group in groups] == [DEFAULT_GROUP_ID, "group:team"]


def test_normalize_schedule_target_ids_deduplicates_values() -> None:
    assert normalize_schedule_target_ids(["group:team", "screen-1", "group:team", " "]) == ["group:team", "screen-1"]


def test_expand_target_ids_resolves_groups_and_individual_screens() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [ScreenGroup.from_dict({"id": "group:team", "name": "Team", "screen_ids": ["screen-2", "screen-3"]})],
    )
    assert expand_target_ids(["group:team", "screen-4", DEFAULT_GROUP_ID], groups) == [
        "screen-2",
        "screen-3",
        "screen-4",
        "screen-1",
    ]


def test_expand_target_ids_deduplicates_group_and_direct_members() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [ScreenGroup.from_dict({"id": "group:team", "name": "Team", "screen_ids": ["screen-2"]})],
    )
    assert expand_target_ids(["group:team", "screen-2"], groups) == ["screen-2"]


def test_expand_target_ids_ignores_missing_groups() -> None:
    assert expand_target_ids(["group:missing", "screen-1"], []) == ["screen-1"]


def test_target_contains_screen_checks_group_membership() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [ScreenGroup.from_dict({"id": "group:team", "name": "Team", "screen_ids": ["screen-2"]})],
    )
    assert target_contains_screen(["group:team"], "screen-2", groups) is True
    assert target_contains_screen([DEFAULT_GROUP_ID], "screen-1", groups) is True
    assert target_contains_screen(["group:team"], "screen-3", groups) is False


def test_target_label_map_returns_group_names() -> None:
    groups = combined_screen_groups(
        ["screen-1"],
        [ScreenGroup.from_dict({"id": "group:team", "name": "Team", "screen_ids": ["screen-2"]})],
    )
    assert target_label_map(groups) == {
        DEFAULT_GROUP_ID: DEFAULT_GROUP_NAME,
        "group:team": "Team",
    }
