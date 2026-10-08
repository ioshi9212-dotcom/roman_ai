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
        if entry.get("operation") == "append":
            # A journal append changes only a file suffix. The original byte
            # length is sufficient for rollback; no huge backup copy needed.
            if entry.get("backup_exists"):
                if not target.exists():
                    raise RuntimeError(f"missing appended transaction target: {entry['target']}")
                with target.open("r+b") as handle:
                    handle.truncate(int(entry["original_size"]))
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                target.unlink(missing_ok=True)
            _fsync_dir(target.parent)
        elif entry.get("backup_exists"):
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
    """Commit replacements and append-only journals in one recoverable transaction."""
    append_values = append_values or {}
    if not values and not append_values:
        return
    if set(values).intersection(append_values):
        raise ValueError("transaction target cannot be replaced and appended at once")
    root = root.resolve()
    with session_transaction(root):
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
            for index, relative in enumerate(sorted(values)):
                relative_path = _safe_relative(relative)
                target = (root / relative_path).resolve()
                target.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = staged_dir / f"{index:04d}.txt"
                backup = backup_dir / f"{index:04d}.txt"
                _write_text_atomic(staged, values[relative])
                backup_exists = target.exists()
                if backup_exists:
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
                    }
                )
            for index, relative in enumerate(sorted(append_values), start=len(values)):
                relative_path = _safe_relative(relative)
                target = (root / relative_path).resolve()
                target.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = staged_dir / f"{index:04d}.txt"
                _write_text_atomic(staged, append_values[relative])
                manifest["entries"].append(
                    {
                        "target": relative,
                        "operation": "append",
                        "staged": str(staged.relative_to(tx)),
                        "backup_exists": target.exists(),
                        "original_size": target.stat().st_size if target.exists() else 0,
                    }
                )
            _fsync_dir(staged_dir)
            _fsync_dir(backup_dir)
            _write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            _fsync_dir(tx)
            for entry in manifest["entries"]:
                target = (root / _safe_relative(str(entry["target"]))).resolve()
                staged = tx / str(entry["staged"])
                if entry.get("operation") == "append":
                    with target.open("ab") as handle, staged.open("rb") as source:
                        shutil.copyfileobj(source, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
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
