from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from .errors import EmptyUploadError, UploadTooLargeError

_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


@dataclass(frozen=True)
class SavedUpload:
    path: Path
    filename: str
    size_bytes: int
    sha256: str


def safe_filename(filename: str | None) -> str:
    """Strip paths and control characters from an untrusted upload name."""
    candidate = (filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = "".join(char for char in candidate if char.isprintable()).strip(" .")
    return (candidate or "document")[:255]


def safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix
    return suffix.lower() if _SAFE_SUFFIX.fullmatch(suffix) else ".bin"


async def save_upload(
    upload: UploadFile,
    destination: Path,
    max_bytes: int,
) -> SavedUpload:
    """Stream an upload to disk while enforcing a hard size limit."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(upload.filename)
    digest = hashlib.sha256()
    total = 0

    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLargeError(
                        f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit"
                    )
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if total == 0:
        destination.unlink(missing_ok=True)
        raise EmptyUploadError("File is empty")

    destination.chmod(0o600)

    return SavedUpload(
        path=destination,
        filename=filename,
        size_bytes=total,
        sha256=digest.hexdigest(),
    )
