from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


CLUSTER_SIZE = 1024
FAT_ENTRIES_PER_CLUSTER = CLUSTER_SIZE // 4
MODE_FILE = 0x0010
MODE_SUBDIR = 0x0020
MODE_EXISTS = 0x8000


class PageReader(Protocol):
    def read_page(self, page_number: int) -> bytes: ...


@dataclass(frozen=True)
class LiveEntry:
    name: str
    mode: int
    length: int
    cluster: int
    raw: bytes

    @property
    def exists(self) -> bool:
        return bool(self.mode & MODE_EXISTS)

    @property
    def is_directory(self) -> bool:
        return bool(self.mode & MODE_SUBDIR)

    @property
    def is_file(self) -> bool:
        return bool(self.mode & MODE_FILE)


class LiveCardCache:
    """Builds a sparse image containing only filesystem metadata and browsed files."""

    def __init__(
        self,
        reader: PageReader,
        output: Path,
        progress: Callable[[int, str], None] | None = None,
    ) -> None:
        self.reader = reader
        self.output = output
        self.progress = progress or (lambda _value, _text: None)
        self.page_size = 512
        self.pages_per_cluster = 2
        self.pages_per_block = 16
        self.clusters_per_card = 0
        self.alloc_offset = 0
        self.alloc_end = 0
        self.root_cluster = 0
        self.backup_block_2 = 0
        self.capacity = 0
        self._cached_pages: set[int] = set()
        self._cluster_cache: dict[int, bytes] = {}
        self._fat_clusters: list[bytes] = []
        self.directories: dict[str, list[LiveEntry]] = {}
        self._file = None

    def __enter__(self) -> "LiveCardCache":
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.output.open("w+b")
        return self

    def __exit__(self, *_args: object) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def _read_page(self, page_number: int) -> bytes:
        data = self.reader.read_page(page_number)[: self.page_size]
        if len(data) != self.page_size:
            raise RuntimeError(f"Page {page_number} was shorter than expected")
        if page_number not in self._cached_pages:
            assert self._file is not None
            self._file.seek(page_number * self.page_size)
            self._file.write(data)
            self._cached_pages.add(page_number)
        return data

    def _read_cluster(self, cluster: int) -> bytes:
        if cluster in self._cluster_cache:
            return self._cluster_cache[cluster]
        data = b"".join(
            self._read_page(cluster * self.pages_per_cluster + page)
            for page in range(self.pages_per_cluster)
        )
        self._cluster_cache[cluster] = data
        return data

    @staticmethod
    def _entry(data: bytes) -> LiveEntry:
        mode = struct.unpack_from("<H", data, 0)[0]
        length = struct.unpack_from("<I", data, 4)[0]
        cluster = struct.unpack_from("<I", data, 16)[0]
        name = data[64:96].split(b"\0", 1)[0].decode("ascii", "replace")
        return LiveEntry(name, mode, length, cluster, data)

    def _fat_entry(self, cluster: int) -> int:
        table_index, entry_index = divmod(cluster, FAT_ENTRIES_PER_CLUSTER)
        if table_index >= len(self._fat_clusters):
            raise RuntimeError(f"FAT entry {cluster} is outside the card filesystem")
        return struct.unpack_from("<I", self._fat_clusters[table_index], entry_index * 4)[0]

    def _chain(self, first_cluster: int, count: int) -> list[int]:
        result: list[int] = []
        current = first_cluster
        for index in range(count):
            if current >= self.alloc_end:
                raise RuntimeError("Invalid cluster chain in memory card filesystem")
            result.append(current)
            if index + 1 < count:
                following = self._fat_entry(current)
                if following == 0xFFFFFFFF:
                    raise RuntimeError("A memory card cluster chain ended early")
                current = following & 0x7FFFFFFF
        return result

    def _read_directory(self, first_cluster: int, entry_count: int) -> list[LiveEntry]:
        cluster_count = max(1, math.ceil(entry_count * 512 / CLUSTER_SIZE))
        raw = b"".join(
            self._read_cluster(self.alloc_offset + cluster)
            for cluster in self._chain(first_cluster, cluster_count)
        )
        return [
            self._entry(raw[offset : offset + 512])
            for offset in range(0, entry_count * 512, 512)
        ]

    def _cache_file(self, entry: LiveEntry) -> bytes:
        if not entry.is_file or not entry.exists or entry.length == 0:
            return b""
        cluster_count = math.ceil(entry.length / CLUSTER_SIZE)
        data = b"".join(
            self._read_cluster(self.alloc_offset + cluster)
            for cluster in self._chain(entry.cluster, cluster_count)
        )
        return data[: entry.length]

    def _load_superblock_and_fat(self) -> None:
        superblock = self._read_page(0)
        if superblock[:28] != b"Sony PS2 Memory Card Format ":
            raise RuntimeError("The inserted card is not PS2-formatted")
        self.page_size, self.pages_per_cluster, self.pages_per_block = struct.unpack_from(
            "<HHH", superblock, 40
        )
        self.clusters_per_card = struct.unpack_from("<I", superblock, 48)[0]
        self.alloc_offset = struct.unpack_from("<I", superblock, 52)[0]
        self.alloc_end = struct.unpack_from("<I", superblock, 56)[0]
        self.root_cluster = struct.unpack_from("<I", superblock, 60)[0]
        self.backup_block_2 = struct.unpack_from("<I", superblock, 68)[0]
        self.capacity = (
            self.clusters_per_card * self.pages_per_cluster * self.page_size
        )
        assert self._file is not None
        self._file.truncate(self.capacity)
        for page in range(1, self.pages_per_block):
            self._read_page(page)
        for page in range(2):
            self._read_page(self.backup_block_2 * self.pages_per_block + page)

        fat_cluster_count = math.ceil(
            self.clusters_per_card / FAT_ENTRIES_PER_CLUSTER
        )
        ifc_count = math.ceil(fat_cluster_count / FAT_ENTRIES_PER_CLUSTER)
        ifc_clusters = struct.unpack_from(f"<{ifc_count}I", superblock, 80)
        fat_locations: list[int] = []
        for ifc_cluster in ifc_clusters:
            indirect = self._read_cluster(ifc_cluster)
            remaining = fat_cluster_count - len(fat_locations)
            fat_locations.extend(
                struct.unpack_from(
                    f"<{min(FAT_ENTRIES_PER_CLUSTER, remaining)}I", indirect
                )
            )
        for index, location in enumerate(fat_locations):
            self._fat_clusters.append(self._read_cluster(location))
            if index % 32 == 0:
                percent = 8 + int((index / max(1, fat_cluster_count)) * 22)
                self.progress(percent, "Reading card filesystem")

    def build(self) -> None:
        self.progress(3, "Reading card filesystem")
        self._load_superblock_and_fat()

        root_first = self._read_cluster(self.alloc_offset + self.root_cluster)
        root_count = self._entry(root_first[:512]).length
        root = self._read_directory(self.root_cluster, root_count)
        self.directories["/"] = root

        folders = [
            entry
            for entry in root
            if entry.exists
            and entry.is_directory
            and entry.name not in (".", "..")
        ]
        for index, folder in enumerate(folders):
            entries = self._read_directory(folder.cluster, folder.length)
            self.directories[folder.name] = entries
            percent = 30 + int(((index + 1) / max(1, len(folders))) * 25)
            self.progress(percent, f"Reading save folders  {index + 1}/{len(folders)}")

        for index, folder in enumerate(folders):
            files = self.directories[folder.name]
            by_name = {entry.name: entry for entry in files if entry.exists}
            icon_sys = next(
                (entry for entry in files if entry.name.lower() == "icon.sys"),
                None,
            )
            if icon_sys:
                icon_data = self._cache_file(icon_sys)
                names = {
                    icon_data[offset : offset + 64]
                    .split(b"\0", 1)[0]
                    .decode("ascii", "replace")
                    for offset in (260, 324, 388)
                }
                for name in names:
                    if name in by_name:
                        self._cache_file(by_name[name])
            percent = 55 + int(((index + 1) / max(1, len(folders))) * 43)
            self.progress(percent, f"Reading save icons  {index + 1}/{len(folders)}")

        assert self._file is not None
        self._file.flush()
        self.progress(100, "Ready")

    def cache_directory_files(self, folder: str) -> None:
        for entry in self.directories.get(folder, []):
            self._cache_file(entry)
        assert self._file is not None
        self._file.flush()
