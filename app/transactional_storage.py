from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.RLock] = {}
_LOCAL = threading.local()


def _lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe transaction path")
    return path


def _rollback(root: Path, transaction_dir: Path, manifest: Dict[str, Any]) -> None:
    for entry in reversed(manifest.get("entries", [])):
        target = (root / _safe_relative(str(entry["target"]))).resolve()
        target.relative_to(root.resolve())
        if entry.get("append"):
            if entry.get("backup_exists"):
                with target.open("r+b") as handle:
                    handle.truncate(int(entry["previous_size"]))
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                target.unlink(missing_ok=True)
            _fsync_dir(target.parent)
            continue
        if entry.get("backup_exists"):
            backup = transaction_dir / str(entry["backup"])
            if not backup.exists():
                raise RuntimeError(f"missing transaction backup for {entry['target']}")
            _write_text_atomic(target, backup.read_text(encoding="utf-8"))
        else:
            target.unlink(missing_ok=True)
            _fsync_dir(target.parent)
    shutil.rmtree(transaction_dir, ignore_errors=True)
    _fsync_dir(transaction_dir.parent)


def recover(root: Path) -> None:
    transactions = root / ".transactions"
    if not transactions.exists():
        return
    for transaction_dir in sorted(path for path in transactions.iterdir() if path.is_dir()):
        manifest_path = transaction_dir / "manifest.json"
        if not manifest_path.exists():
            shutil.rmtree(transaction_dir, ignore_errors=True)
            _fsync_dir(transactions)
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("state") == "committed":
            shutil.rmtree(transaction_dir, ignore_errors=True)
            _fsync_dir(transactions)
        else:
            _rollback(root, transaction_dir, manifest)
    try:
        transactions.rmdir()
        _fsync_dir(root)
    except OSError:
        pass


@contextmanager
def session_transaction(root: Path) -> Iterator[None]:
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(str(root))
    key = str(root)
    active = set(getattr(_LOCAL, "active", set()))
    if key in active:
        yield
        return
    with _lock_for(root):
        lock_path = root / ".session.lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("a+") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            active = set(getattr(_LOCAL, "active", set()))
            active.add(key)
            _LOCAL.active = active
            try:
                recover(root)
                from .session_documents import read_view
                with read_view():
                    yield
            finally:
                active = set(getattr(_LOCAL, "active", set()))
                active.discard(key)
                _LOCAL.active = active
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_batch(
    root: Path, values: Dict[str, str], *,
    append_values: Dict[str, str] | None = None,
) -> None:
    if not values and not append_values:
        return
    root = root.resolve()
    with session_transaction(root):
        from .session_documents import encode_batch, file_path
        values = encode_batch(root, values)
        append = {
            str(file_path(root, name).relative_to(root)): text
            for name, text in (append_values or {}).items()
        }
        if set(values) & set(append):
            raise ValueError("Cannot replace and append to the same file in one transaction")
        # Canonical documents are not rewritten when the decoded content is unchanged.
        values = {name: text for name, text in values.items()
                  if ((root / name).is_file() if text is None else
                      not (root / name).is_file() or (root / name).read_text(encoding="utf-8") != text)}
        if not values and not append:
            return
        transactions = root / ".transactions"
        tx = transactions / uuid.uuid4().hex
        staged_dir = tx / "staged"
        backup_dir = tx / "backup"
        staged_dir.mkdir(parents=True, exist_ok=False)
        backup_dir.mkdir(parents=True, exist_ok=True)
        _fsync_dir(transactions)
        _fsync_dir(tx)
        manifest: Dict[str, Any] = {"version": 1, "state": "prepared", "entries": []}
        manifest_path = tx / "manifest.json"
        try:
            for index, relative in enumerate(sorted(set(values) | set(append))):
                relative_path = _safe_relative(relative)
                target = (root / relative_path).resolve()
                target.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = staged_dir / f"{index:04d}.txt"
                backup = backup_dir / f"{index:04d}.txt"
                is_append = relative in append
                if is_append:
                    _write_text_atomic(staged, append[relative])
                elif values[relative] is not None:
                    _write_text_atomic(staged, values[relative])
                backup_exists = target.exists()
                previous_size = target.stat().st_size if is_append and backup_exists else 0
                if backup_exists and not is_append:
                    with target.open("rb") as source, backup.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                        dest.flush()
                        os.fsync(dest.fileno())
                manifest["entries"].append(
                    {
                        "target": relative,
                        "staged": str(staged.relative_to(tx)),
                        "backup": str(backup.relative_to(tx)),
                        "backup_exists": backup_exists,
                        "previous_size": previous_size if is_append else None,
                        "append": is_append,
                        "delete": False if is_append else values[relative] is None,
                    }
                )
            _fsync_dir(staged_dir)
            _fsync_dir(backup_dir)
            _write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            _fsync_dir(tx)
            for entry in manifest["entries"]:
                target = (root / _safe_relative(str(entry["target"]))).resolve()
                staged = tx / str(entry["staged"])
                if entry.get("append"):
                    # The prepared journal can undo even a partial append on
                    # crash by truncating to previous_size during recovery.
                    with target.open("ab") as handle:
                        handle.write(staged.read_bytes())
                        handle.flush()
                        os.fsync(handle.fileno())
                elif entry.get("delete"):
                    target.unlink(missing_ok=True)
                else:
                    os.replace(staged, target)
                _fsync_dir(target.parent)
            manifest["state"] = "committed"
            _write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            shutil.rmtree(tx, ignore_errors=True)
            _fsync_dir(transactions)
            try:
                transactions.rmdir()
                _fsync_dir(root)
            except OSError:
                pass
        except Exception:
            try:
                if manifest_path.exists():
                    current = json.loads(manifest_path.read_text(encoding="utf-8"))
                    _rollback(root, tx, current)
                else:
                    shutil.rmtree(tx, ignore_errors=True)
                    _fsync_dir(transactions)
            finally:
                raise


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
