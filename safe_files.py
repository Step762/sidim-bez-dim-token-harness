"""Bounded local inputs opened relative to directory descriptors on Linux."""

from contextlib import contextmanager
from contextvars import ContextVar
import io
import os
from pathlib import Path
import stat


TASK_JSON_BYTES = 1024 * 1024
MARKDOWN_BYTES = 1024 * 1024
KB_BYTES = 16 * 1024 * 1024
CSV_BYTES = 64 * 1024 * 1024
MAX_TREE_ENTRIES = 4096
MAX_TREE_DEPTH = 32
MAX_KB_FILES = 1024


class UnsafeFile(ValueError):
    """An input is not a bounded regular file in the permitted directory."""


_guards = ContextVar("input_directory_identities", default=None)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def identity(info):
    return info.st_dev, info.st_ino


@contextmanager
def directory_guards(directories):
    guards = dict(_guards.get() or {})
    guards.update(directories)
    token = _guards.set(guards)
    try:
        yield guards
    finally:
        _guards.reset(token)


def _parts(relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise UnsafeFile("input points outside the permitted directory")
    return path.parts


@contextmanager
def open_directory(path):
    """Open every component without following links, checking guarded roots."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise UnsafeFile("directory points outside the permitted root")
    fd = os.open(path.anchor, _DIRECTORY_FLAGS)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise UnsafeFile("symbolic link or non-directory in input path")
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            current /= part
            after = os.fstat(fd)
            if identity(before) != identity(after):
                raise UnsafeFile("input directory changed during open")
            expected = (_guards.get() or {}).get(current)
            if expected is not None and identity(after) != expected:
                raise UnsafeFile("input task directory was replaced")
        yield fd
    finally:
        os.close(fd)


def _regular(info, maximum):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeFile("input must be a regular file without symbolic or hard links")
    if info.st_size > maximum:
        raise UnsafeFile("input file exceeds the byte limit")


def _version(info):
    return identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _BoundedReader(io.RawIOBase):
    def __init__(self, raw, maximum):
        super().__init__()
        self.raw, self.remaining = raw, maximum

    def readable(self):
        return True

    def readinto(self, buffer):
        # The extra byte distinguishes EOF from a file that grew past its limit.
        count = self.raw.readinto(memoryview(buffer)[:min(len(buffer), self.remaining + 1)])
        if count > self.remaining:
            raise UnsafeFile("input file exceeds the byte limit while reading")
        self.remaining -= count
        return count


@contextmanager
def open_text(root, relative, *, maximum, encoding="utf-8", newline=None):
    parts = _parts(relative)
    if not parts:
        raise UnsafeFile("missing input filename")
    with open_directory(Path(root).joinpath(*parts[:-1])) as parent:
        before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        _regular(before, maximum)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        with io.FileIO(fd, mode="rb", closefd=True) as raw:
            opened = os.fstat(raw.fileno())
            _regular(opened, maximum)
            if _version(before) != _version(opened):
                raise UnsafeFile("input file changed during open")
            with io.TextIOWrapper(io.BufferedReader(_BoundedReader(raw, maximum)),
                                  encoding=encoding, newline=newline) as stream:
                yield stream
                if _version(opened) != _version(os.fstat(raw.fileno())):
                    raise UnsafeFile("input file changed while reading")


def read_text(root, relative, *, maximum, encoding="utf-8"):
    with open_text(root, relative, maximum=maximum, encoding=encoding) as stream:
        return stream.read()


def markdown_files(root):
    """Enumerate a bounded tree without following directory or document links."""
    count = 0
    files = []

    def visit(fd, prefix, depth):
        nonlocal count
        if depth > MAX_TREE_DEPTH:
            raise UnsafeFile("knowledge directory exceeds the depth limit")
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > MAX_TREE_ENTRIES:
                    raise UnsafeFile("knowledge directory exceeds the entry limit")
                info = entry.stat(follow_symlinks=False)
                relative = prefix / entry.name
                if stat.S_ISLNK(info.st_mode):
                    raise UnsafeFile("symbolic links in knowledge directories are unsupported")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=fd)
                    try:
                        if identity(os.fstat(child)) != identity(info):
                            raise UnsafeFile("knowledge directory changed during traversal")
                        visit(child, relative, depth + 1)
                    finally:
                        os.close(child)
                elif relative.suffix == ".md":
                    _regular(info, MARKDOWN_BYTES)
                    files.append(relative)
                    if len(files) > MAX_KB_FILES:
                        raise UnsafeFile("knowledge directory exceeds the document limit")

    with open_directory(root) as fd:
        visit(fd, Path(), 0)
    return sorted(files)
