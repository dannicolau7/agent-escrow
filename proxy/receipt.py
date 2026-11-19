"""Signed receipts for approved actions.

A receipt says: a named person approved this exact tool call, at this time, on this escrow.
It is signed with the same Ed25519 key as audit checkpoints, so anyone holding the public
key can check it, and nobody can produce one without the private key.

It is for handing to the other side of an action. An agent asking a service to do something
can attach the receipt; the service checks the signature and that the arguments it received
are the ones that were approved.

    python -m proxy.receipt verify receipt.json --pubkey audit-signing.key.pub \\
        [--arguments call-arguments.json]

What a receipt does not say: that the approval was wise, that the action has run, or who the
approver is beyond the label of the key they used. The arguments are not in the receipt,
only their SHA-256, so a receipt can be shown without disclosing them.

Receipts are issued only for actions a reviewer approved. Calls that passed without review,
and actions that were rejected, timed out or cancelled, have none.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import checkpoint

FORMAT = "agent-escrow-approval-receipt/1"
SIGNED_FIELDS = (
    "format",
    "action_id",
    "decision",
    "tool_name",
    "arguments_sha256",
    "risk_level",
    "submitted_by",
    "approved_by",
    "requested_at",
    "approved_at",
    "issued_at",
)
# Not a person's decision: the proxy's own rejections, and a request with no known key.
_NOT_A_REVIEWER = ("", "system", "unknown")


class ReceiptError(RuntimeError):
    """A receipt cannot be issued. The message says why and is safe to show."""


class NoReceipt(ReceiptError):
    """This action is not one a reviewer approved, so it has no receipt."""


def arguments_digest(arguments: Any) -> str:
    """SHA-256 of a call's arguments as canonical JSON. Both sides compute it the same way."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def message(fields: dict[str, Any]) -> bytes:
    """The exact bytes that are signed."""
    return json.dumps({k: fields[k] for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")).encode()


def issue(action: dict[str, Any]) -> dict[str, Any]:
    """Sign a receipt for an action row (db.get_action). Refuses anything not approved by a reviewer."""
    path = checkpoint.signing_key_path()
    if path is None:
        raise ReceiptError("no signing key is configured (ESCROW_AUDIT_SIGNING_KEY)")
    if action.get("status") != "APPROVED":
        raise NoReceipt("only an approved action has a receipt")
    approver = (action.get("resolved_by") or "").strip()
    if approver in _NOT_A_REVIEWER or not action.get("resolved_at"):
        raise NoReceipt("this action has no recorded reviewer")
    try:
        arguments = json.loads(action["raw_arguments"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReceiptError("the stored arguments of this action cannot be read") from exc
    try:
        key = checkpoint._load_private(path)
    except checkpoint.CheckpointError as exc:
        raise ReceiptError(str(exc)) from exc
    fields = {
        "format": FORMAT,
        "action_id": action["id"],
        "decision": "APPROVED",
        "tool_name": action["tool_name"],
        "arguments_sha256": arguments_digest(arguments),
        "risk_level": action["risk_level"],
        "submitted_by": action.get("submitted_by") or "",
        "approved_by": approver,
        "requested_at": action["created_at"],
        "approved_at": action["resolved_at"],
        "issued_at": datetime.now(UTC).isoformat(),
    }
    public_pem = checkpoint._public_pem(key)
    return {
        **fields,
        "signature": base64.b64encode(key.sign(message(fields))).decode(),
        "public_key": public_pem,
        "public_key_fingerprint": checkpoint.fingerprint(public_pem),
    }


_NO_ARGUMENTS = object()


def problems(receipt: Any, trusted_public_pem: str, arguments: Any = _NO_ARGUMENTS) -> list[str]:
    """Everything wrong with a receipt. An empty list means it holds.

    The public key inside a receipt is a convenience and is never trusted: the caller
    supplies the key it obtained separately. Pass `arguments` to also confirm that a call's
    arguments are the ones that were approved.
    """
    if not isinstance(receipt, dict) or receipt.get("format") != FORMAT:
        return ["this is not an approval receipt"]
    serialization, _, public_cls, invalid = checkpoint._crypto()
    try:
        key = serialization.load_pem_public_key(trusted_public_pem.encode())
        if not isinstance(key, public_cls):
            return ["the public key is not an Ed25519 key"]
    except (ValueError, TypeError):
        return ["the public key cannot be read"]
    try:
        key.verify(base64.b64decode(receipt["signature"], validate=True), message(receipt))
    except (invalid, KeyError, ValueError, TypeError):
        return ["the signature does not match: the receipt was changed, or another key signed it"]
    found = []
    if receipt["decision"] != "APPROVED" or receipt["approved_by"] in _NOT_A_REVIEWER:
        found.append("the receipt does not record an approval by a reviewer")
    if arguments is not _NO_ARGUMENTS and arguments_digest(arguments) != receipt["arguments_sha256"]:
        found.append("these are not the arguments that were approved")
    return found


def main(argv: list[str]) -> int:
    usage = "usage: python -m proxy.receipt verify RECEIPT.json --pubkey KEY.pub [--arguments ARGS.json]\n"
    if len(argv) not in (4, 6) or argv[0] != "verify" or argv[2] != "--pubkey":
        sys.stderr.write(usage)
        return 2
    if len(argv) == 6 and argv[4] != "--arguments":
        sys.stderr.write(usage)
        return 2
    try:
        receipt = json.loads(Path(argv[1]).read_text())
        public_pem = Path(argv[3]).read_text()
        arguments = json.loads(Path(argv[5]).read_text()) if len(argv) == 6 else _NO_ARGUMENTS
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"cannot read the files: {exc}\n")
        return 2
    found = problems(receipt, public_pem, arguments)
    if found:
        sys.stdout.write("RECEIPT NOT VALID\n" + "".join(f"  - {p}\n" for p in found))
        return 1
    sys.stdout.write(
        f"Receipt valid: {receipt['approved_by']} approved {receipt['tool_name']} "
        f"(action {receipt['action_id']}) at {receipt['approved_at']}\n"
        f"Signed by key {checkpoint.fingerprint(public_pem)}\n"
    )
    if arguments is _NO_ARGUMENTS:
        sys.stdout.write("The call's arguments were not checked: pass --arguments to compare them.\n")
    else:
        sys.stdout.write("The arguments match what was approved.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
