from __future__ import annotations

from pathlib import Path

from media_library import (
    build_media_library_index,
    build_media_library_index_from_paths,
    media_category_label,
    media_relative_path,
)


def test_build_media_library_index_filters_and_sorts_files(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.jpg").write_bytes(b"x")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.mp4").write_bytes(b"x")

    index = build_media_library_index(tmp_path)

    assert index.file_count == 2
    assert [record.relative_path for record in index.records] == [
        "b.jpg",
        "nested/a.mp4",
    ]


def test_build_media_library_index_from_paths_payload(tmp_path: Path) -> None:
    media = tmp_path / "poster.png"
    media.write_bytes(b"x")
    index = build_media_library_index_from_paths(tmp_path, [media])

    payload = index.payload()
    assert payload == [
        {
            "relativePath": "poster.png",
            "name": "poster.png",
            "category": "Root",
            "isVideo": False,
            "isImage": True,
        }
    ]


def test_media_relative_path_and_category_label(tmp_path: Path) -> None:
    sub = tmp_path / "lobby"
    sub.mkdir()
    media = sub / "welcome.webp"
    media.write_bytes(b"x")

    assert media_relative_path(media, tmp_path) == "lobby/welcome.webp"
    assert media_category_label(media, tmp_path) == "lobby"
