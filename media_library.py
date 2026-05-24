from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".ogg"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS


@dataclass(frozen=True, slots=True)
class MediaLibraryRecord:
    absolute_path: Path
    relative_path: str
    name: str
    category: str
    is_video: bool
    is_image: bool


@dataclass(slots=True)
class MediaLibraryIndex:
    root: Path | None
    records: list[MediaLibraryRecord]

    @property
    def file_count(self) -> int:
        return len(self.records)

    @property
    def paths(self) -> list[Path]:
        return [record.absolute_path for record in self.records]

    def payload(self) -> list[dict[str, object]]:
        return [
            {
                "relativePath": record.relative_path,
                "name": record.name,
                "category": record.category,
                "isVideo": record.is_video,
                "isImage": record.is_image,
            }
            for record in self.records
        ]


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def media_relative_path(path: Path, directory: Path | None) -> str:
    if directory is None:
        return path.name
    return path.relative_to(directory).as_posix()


def media_category_label(path: Path, directory: Path | None) -> str:
    relative = Path(media_relative_path(path, directory))
    parent = relative.parent.as_posix()
    return parent if parent not in {"", "."} else "Root"


def scan_video_directory(directory: Path | None) -> list[Path]:
    return build_media_library_index(directory).paths


def build_media_library_index_from_paths(
    directory: Path | None,
    paths: list[Path],
) -> MediaLibraryIndex:
    records = [
        MediaLibraryRecord(
            absolute_path=path,
            relative_path=media_relative_path(path, directory),
            name=path.name,
            category=media_category_label(path, directory),
            is_video=is_video_file(path),
            is_image=is_image_file(path),
        )
        for path in paths
    ]
    return MediaLibraryIndex(root=directory, records=records)


def build_media_library_index(directory: Path | None) -> MediaLibraryIndex:
    if directory is None or not directory.exists() or not directory.is_dir():
        return MediaLibraryIndex(root=directory, records=[])
    paths = sorted(
        [
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
        ],
        key=lambda path: (
            path.relative_to(directory).parent.as_posix().lower(),
            path.name.lower(),
        ),
    )
    records = [
        MediaLibraryRecord(
            absolute_path=path,
            relative_path=media_relative_path(path, directory),
            name=path.name,
            category=media_category_label(path, directory),
            is_video=is_video_file(path),
            is_image=is_image_file(path),
        )
        for path in paths
    ]
    return MediaLibraryIndex(root=directory, records=records)
