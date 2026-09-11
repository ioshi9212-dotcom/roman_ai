from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict


RECEIPTS_FILE = "operation_receipts.json"
RECEIPTS_VERSION = 1
MAX_RECEIPTS = 128


class OperationReceiptConflict(RuntimeError):
    pass


def canonical_hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_fingerprint(operation: str, identity: str, payload: Dict[str, Any]) -> str:
    return canonical_hash({
        "operation": str(operation),
        "identity": str(identity),
        "payload": payload,
    })


def receipt_key(operation: str, identity: str) -> str:
    return f"{operation}:{identity}"


def _normalise_ledger(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    receipts = value.get("receipts")
    if not isinstance(receipts, dict):
        receipts = {}
    order = value.get("order")
    if not isinstance(order, list):
        order = list(receipts)
    order = [str(key) for key in order if str(key) in receipts]
    for key in receipts:
        if key not in order:
            order.append(key)
    return {
        "version": RECEIPTS_VERSION,
        "receipts": deepcopy(receipts),
        "order": order,
    }


def load_ledger(root: Path) -> Dict[str, Any]:
    path = root / RECEIPTS_FILE
    if not path.exists():
        return _normalise_ledger({})
    try:
        return _normalise_ledger(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _normalise_ledger({})


def find_receipt(root: Path, operation: str, identity: str) -> Dict[str, Any] | None:
    ledger = load_ledger(root)
    row = ledger["receipts"].get(receipt_key(operation, identity))
    return deepcopy(row) if isinstance(row, dict) else None


def replay_result(
    root: Path,
    *,
    operation: str,
    identity: str,
    fingerprint: str,
) -> Dict[str, Any] | None:
    receipt = find_receipt(root, operation, identity)
    if receipt is None:
        return None
    if str(receipt.get("request_fingerprint") or "") != str(fingerprint):
        raise OperationReceiptConflict("IDEMPOTENCY_KEY_REUSED")
    result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
    replay = deepcopy(result)
    replay["ok"] = True
    replay["already_completed"] = True
    replay["idempotent_replay"] = True
    replay["operation_identity"] = identity
    return replay


def make_receipt(
    *,
    operation: str,
    identity: str,
    fingerprint: str,
    result: Dict[str, Any],
    turn_number: int | None = None,
    audited_through: int | None = None,
    target_turn_after: int | None = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "operation": str(operation),
        "identity": str(identity),
        "request_fingerprint": str(fingerprint),
        "completed": True,
        "result": deepcopy(result),
    }
    if turn_number is not None:
        row["turn_number"] = int(turn_number)
    if audited_through is not None:
        row["audited_through"] = int(audited_through)
    if target_turn_after is not None:
        row["target_turn_after"] = int(target_turn_after)
    return row


def ledger_with_receipt(root: Path, receipt: Dict[str, Any]) -> Dict[str, Any]:
    ledger = load_ledger(root)
    operation = str(receipt.get("operation") or "")
    identity = str(receipt.get("identity") or "")
    if not operation or not identity:
        raise ValueError("operation receipt requires operation and identity")
    key = receipt_key(operation, identity)
    existing = ledger["receipts"].get(key)
    if isinstance(existing, dict):
        old = str(existing.get("request_fingerprint") or "")
        new = str(receipt.get("request_fingerprint") or "")
        if old and new and old != new:
            raise OperationReceiptConflict("IDEMPOTENCY_KEY_REUSED")
    ledger["receipts"][key] = deepcopy(receipt)
    ledger["order"] = [item for item in ledger["order"] if item != key] + [key]
    while len(ledger["order"]) > MAX_RECEIPTS:
        removed = ledger["order"].pop(0)
        ledger["receipts"].pop(removed, None)
    return ledger


def prune_after_turn(ledger: Dict[str, Any], target_turn: int) -> Dict[str, Any]:
    value = _normalise_ledger(ledger)
    keep: Dict[str, Any] = {}
    order = []
    for key in value["order"]:
        row = value["receipts"].get(key)
        if not isinstance(row, dict):
            continue
        operation = str(row.get("operation") or "")
        if operation == "commit_turn" and int(row.get("turn_number", 0) or 0) > target_turn:
            continue
        if operation == "commit_audit" and int(row.get("audited_through", 0) or 0) > target_turn:
            continue
        keep[key] = deepcopy(row)
        order.append(key)
    return {"version": RECEIPTS_VERSION, "receipts": keep, "order": order}


def turn_identity(turn: Any) -> str | None:
    if not isinstance(turn, dict):
        return None
    stable = deepcopy(turn)
    stable.pop("saved_at", None)
    extracted = stable.get("extracted")
    if isinstance(extracted, dict):
        extracted = deepcopy(extracted)
        extracted.pop("_commit_request_fingerprint", None)
        stable["extracted"] = extracted
    return canonical_hash(stable)


def current_turn_identity(root: Path) -> str | None:
    path = root / "turns.jsonl"
    if not path.exists():
        return None
    last: Dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            last = row
    return turn_identity(last)
