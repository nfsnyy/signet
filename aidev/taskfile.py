import json
from pathlib import Path

from .errors import PlatformError
from .jsonio import atomic_write_bytes


def dumps(metadata, body):
    header = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return f"---\n{header}\n---\n{body}".encode("utf-8")


def loads(content):
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PlatformError("task.invalid_encoding", "task.md must be UTF-8") from error
    if not text.startswith("---\n"):
        raise PlatformError("task.invalid_frontmatter", "task.md must start with JSON frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise PlatformError("task.invalid_frontmatter", "task.md frontmatter is not closed")
    try:
        metadata = json.loads(text[4:end])
    except json.JSONDecodeError as error:
        raise PlatformError("task.invalid_frontmatter", "task.md frontmatter must be valid JSON") from error
    if not isinstance(metadata, dict):
        raise PlatformError("task.invalid_frontmatter", "task.md frontmatter must be a JSON object")
    return metadata, text[end + 5:]


def read_task(path):
    return loads(Path(path).read_bytes())


def write_task(path, metadata, body):
    atomic_write_bytes(path, dumps(metadata, body))
