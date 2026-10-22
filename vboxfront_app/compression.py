"""VBoxFront compression."""

from __future__ import annotations

import lzma
import os

from .constants import COMPRESSORS, STDIN_CHUNK


def compressor_for(path: str):
    """The opener for `path`'s compression, or None when it is not compressed."""
    return COMPRESSORS.get(os.path.splitext(path)[1].lower())


def open_maybe_compressed(path: str):
    """A binary file object for `path`, transparently decompressing it."""
    opener = compressor_for(path)
    return opener(path, "rb") if opener else open(path, "rb")


def uncompressed_size(path: str) -> int | None:
    """The uncompressed length of `path` without reading it, or None.

    Compressed files must be measured with a full streaming read. A gzip
    trailer cannot distinguish a small image from a wrapped or multi-member
    one. None means "measure it".
    """
    if not compressor_for(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return None
    return None


def measure_uncompressed(path: str, progress=None) -> int | None:
    """Stream `path` through its decompressor and count the bytes.

    The honest way to the byte count `convertfromraw stdin` demands when the
    container does not record one. It reads the *compressed* file, which is
    the smaller one, and writes nothing. `progress(uncompressed_read)` is called
    as it goes and may return False to abort, which yields None.
    """
    try:
        with open_maybe_compressed(path) as stream:
            total = 0
            while True:
                chunk = stream.read(STDIN_CHUNK)
                if not chunk:
                    return total
                total += len(chunk)
                if progress is not None and progress(total) is False:
                    return None
    except (OSError, EOFError, ValueError, lzma.LZMAError):
        return None
