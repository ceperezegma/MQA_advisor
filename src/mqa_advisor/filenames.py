"""Filesystem-safe filenames for per-resource files.

A `resource_id` can be a URL (e.g. `https://data.gov.lt/datasets/2916/`), which
contains characters Windows forbids in filenames. On NTFS a `:` doesn't fail on
write — it silently creates an alternate data stream — and the later rename
then dies with `WinError 87`.
"""

import re

_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_file_component(value: str) -> str:
    """Replace characters that are illegal in Windows filenames with `-`."""
    return _UNSAFE_FILENAME_CHARS.sub("-", value)
