"""Signed checkpoints of the audit log.

The audit log is a hash chain: each row's hash covers the row before it, so the hash of the
latest row stands for the whole log up to that point. A checkpoint is that hash, signed with
an Ed25519 private key that only this server holds. Anyone with the matching public key can
check the signature; nobody can produce one without the private key.

A checkpoint proves what this server vouched for at the moment it signed. It becomes proof
against later rewriting once it has been handed to someone outside (an auditor, the system
owner): any later export must still contain that row with that hash.

    python -m proxy.checkpoint keygen audit-signing.key     # writes the key and .pub

Set ESCROW_AUDIT_SIGNING_KEY to the private key's path. Without it, no checkpoints are issued.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT = "agent-escrow-audit-checkpoint/1"
SIGNED_FIELDS = ("format", "last_id", "hash", "rows", "keyed_chain", "signed_at")


class CheckpointError(RuntimeError):
    """Checkpoints cannot be issued or checked. The message says why."""


def signing_key_path() -> Path | None:
    raw = os.environ.get("ESCROW_AUDIT_SIGNING_KEY", "").strip()
    return Path(raw) if raw else None


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - the package is a declared dependency
        raise CheckpointError("the 'cryptography' package is required for checkpoints") from exc
    return serialization, Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature


def _load_private(path: Path):
    serialization, private_cls, _, _ = _crypto()
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        # Never include file contents in the message: it may end up in a log.
        raise CheckpointError(f"cannot load the audit signing key at {path}") from exc
    if not isinstance(key, private_cls):
        raise CheckpointError("the audit signing key must be an Ed25519 private key")
    return key


def _public_pem(private_key) -> str:
    serialization, _, _, _ = _crypto()
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def fingerprint(public_pem: str) -> str:
    """Short form of a public key, for confirming it over a second channel."""
    digest = hashlib.sha256(public_pem.strip().encode()).hexdigest()[:32]
    return ":".join(digest[i : i + 4] for i in range(0, 32, 4))


def message(fields: dict[str, Any]) -> bytes:
    """The exact bytes that are signed: the signed fields as canonical JSON."""
    return json.dumps({k: fields[k] for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")).encode()


def validate_startup() -> None:
    """If a signing key is configured it must be usable; a typo must not go unnoticed."""
    path = signing_key_path()
    if path is not None:
        _load_private(path)


def issue(head: dict[str, Any]) -> dict[str, Any]:
    """Sign the head of the audit chain. `head` is db.audit_head()."""
    path = signing_key_path()
    if path is None:
        raise CheckpointError("no audit signing key is configured (ESCROW_AUDIT_SIGNING_KEY)")
    if not head.get("hash"):
        raise CheckpointError("the audit log has no hashed rows to sign")
    key = _load_private(path)
    fields = {
        "format": FORMAT,
        "last_id": head["last_id"],
        "hash": head["hash"],
        "rows": head["rows"],
        "keyed_chain": bool(os.environ.get("ESCROW_AUDIT_KEY")),
        "signed_at": datetime.now(UTC).isoformat(),
    }
    public_pem = _public_pem(key)
    return {
        **fields,
        "signature": base64.b64encode(key.sign(message(fields))).decode(),
        "public_key": public_pem,
        "public_key_fingerprint": fingerprint(public_pem),
    }


def verify(checkpoint: dict[str, Any], trusted_public_pem: str) -> bool:
    """True if the checkpoint was signed by the holder of `trusted_public_pem`.

    The public key inside a checkpoint is a convenience and is never trusted here: the
    caller supplies the key it obtained separately.
    """
    serialization, _, public_cls, invalid = _crypto()
    try:
        key = serialization.load_pem_public_key(trusted_public_pem.encode())
        if not isinstance(key, public_cls) or checkpoint.get("format") != FORMAT:
            return False
        key.verify(base64.b64decode(checkpoint["signature"], validate=True), message(checkpoint))
    except (invalid, KeyError, ValueError, TypeError):
        return False
    return True


def keygen(path: Path) -> str:
    """Write a new private key (owner-only) and its .pub. Returns the fingerprint."""
    serialization, private_cls, _, _ = _crypto()
    public_path = path.with_name(path.name + ".pub")
    if path.exists() or public_path.exists():
        raise CheckpointError(f"{path} or {public_path} already exists; refusing to overwrite a key")
    key = private_cls.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    public_pem = _public_pem(key)
    public_path.write_text(public_pem)
    return fingerprint(public_pem)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "keygen":
        sys.stderr.write("usage: python -m proxy.checkpoint keygen PATH\n")
        return 2
    try:
        fp = keygen(Path(argv[1]))
    except CheckpointError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(f"private key  {argv[1]}  (keep secret; set ESCROW_AUDIT_SIGNING_KEY to this path)\n")
    sys.stdout.write(f"public key   {argv[1]}.pub  (give to whoever checks your audit log)\n")
    sys.stdout.write(f"fingerprint  {fp}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
