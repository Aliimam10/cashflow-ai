"""Bounded multipart upload reads shared by ingestion routes."""

from __future__ import annotations

from fastapi import UploadFile


async def read_bounded_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    """Read at most one byte beyond the adapter limit and always close the file."""
    try:
        return await upload.read(max_bytes + 1)
    finally:
        await upload.close()


__all__ = ["read_bounded_upload"]
