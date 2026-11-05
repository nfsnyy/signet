import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def canonical_bytes(value):
    return (json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_json(path, value):
    atomic_write_bytes(path, canonical_bytes(value))


def append_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for value in values:
            handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4()}"
