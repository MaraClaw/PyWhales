"""Data-only bounded ZIP and tar decoders with canonical unique paths."""

import io
import stat
import tarfile
import zipfile
import zlib
from pathlib import PurePosixPath

from .api import Rejected, require
from .budgets import Expansion
from .limits import ArchiveLimits


def safe_path(name: str) -> str:
    """Reject noncanonical paths and checkout-control directories."""
    path = PurePosixPath(name)
    require(
        bool(path.parts)
        and not path.is_absolute()
        and all(part not in {"..", ".git", ".github"} for part in path.parts)
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
        and str(path) == name.rstrip("/"),
        "unsafe archive path",
    )
    return str(path)


def zip_files(
    data: bytes, limits: ArchiveLimits, *, directories: set[str] | None = None
) -> dict[str, bytes]:
    """Read bounded ZIP files and optionally collect explicit directories on success."""
    budget = Expansion(limits)
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                budget.entry(entry.file_size)
                require(entry.orig_filename == entry.filename, "unsafe archive path")
                name = safe_path(entry.filename)
                require(name not in seen, "duplicate archive path")
                seen.add(name)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                require(mode in {0, stat.S_IFREG, stat.S_IFDIR}, "archive special file")
                require(
                    (mode != stat.S_IFDIR or entry.is_dir())
                    and (mode != stat.S_IFREG or not entry.is_dir()),
                    "archive special file",
                )
                if entry.is_dir():
                    require(entry.file_size == 0, "archive directory payload")
                    continue
                with archive.open(entry) as stream:
                    files[name] = budget.member(stream)
    except zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error:
        raise Rejected("invalid ZIP archive") from None
    if directories is not None:
        directories.update(seen.difference(files))
    return files


def tar_files(
    data: bytes,
    limits: ArchiveLimits,
    *,
    pages: bool = False,
    directories: set[str] | None = None,
) -> dict[str, bytes]:
    """Read bounded regular tar members, accepting the Pages uploader's ./ root.

    Iterate instead of building an unbounded getmembers list. Tar parser and
    decompressor internal allocations are outside the retained-payload budget.
    Explicit directories are optionally collected after a successful read.
    """
    budget = Expansion(limits)
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(
            fileobj=io.BytesIO(data), mode="r|" if pages else "r|gz"
        ) as archive:
            for member in archive:
                budget.entry(member.size)
                require(
                    member.type in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE},
                    "archive special file",
                )
                raw = member.name.removeprefix("./") if pages else member.name
                name = (
                    "."
                    if pages and raw in {"", "."} and member.isdir()
                    else safe_path(raw)
                )
                require(name not in seen, "duplicate archive path")
                seen.add(name)
                if member.isdir():
                    require(member.size == 0, "archive directory payload")
                    continue
                stream = archive.extractfile(member)
                require(stream is not None, "archive file unreadable")
                assert stream is not None
                with stream:
                    files[name] = budget.member(stream)
    except tarfile.TarError, EOFError, zlib.error:
        raise Rejected("invalid tar archive") from None
    if directories is not None:
        directories.update(seen.difference(files))
    return files
