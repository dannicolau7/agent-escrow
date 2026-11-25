"""Signed requests from agents: which agent is asking, proved on every call.

An API key says "someone who holds this key". Anyone who copies it is that agent, and it
travels with every request. A signature is stronger: each agent holds a private key that
never leaves it, signs every request, and the proxy checks the signature against the public
key registered for that agent. A request that is unsigned, signed by an unknown or revoked
key, changed after signing, sent twice, or too old is refused, recorded in the audit log and
reported to whoever is watching.

The signature is an HTTP Message Signature (RFC 9421) with an Ed25519 key. It covers the
method, the host, the path and a digest of the body, with a creation time, an expiry and a
one-time number:

    Content-Digest:  sha-256=:<base64 of the body's SHA-256>:
    Signature-Input: sig1=("@method" "@authority" "@path" "content-digest");created=...;
                     expires=...;keyid="...";nonce="...";alg="ed25519";tag="agent-escrow"
    Signature:       sig1=:<base64 signature>:

ESCROW_AGENT_SIGNATURES sets how it is used:

    off       (default) signatures are ignored; agents are known by their API key alone
    warn      a missing or bad signature is recorded and reported, and the call goes on.
              For rolling this out: see who would be refused before refusing anyone
    required  a missing or bad signature is refused

The API key is still needed. A key is registered for one agent label, and a signature made
with it is accepted only on a request that carries that agent's API key: stealing either one
alone is not enough.

    python -m proxy.agent_identity keygen bot.key --label bot   # new key pair, registered
    python -m proxy.agent_identity add bot.key.pub --label bot  # register an existing public key
    python -m proxy.agent_identity list
    python -m proxy.agent_identity revoke KEYID --reason "laptop lost"

What a signature does not show: that the agent was not tricked into asking. A manipulated
agent signs a harmful request with its own valid key. Holding risky calls for a person is what
covers that, and it stays on whatever this is set to.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from . import checkpoint, db

LABEL = "sig1"
COVERED = '("@method" "@authority" "@path" "content-digest")'
MAX_AGE_SECONDS = 300
CLOCK_SKEW_SECONDS = 60
MAX_HEADER_CHARS = 1000
# What a key id may look like. It comes from the agent and ends up in logs and alerts.
KEYID = re.compile(r"[A-Za-z0-9:._-]{4,100}")
NONCE = re.compile(r"[A-Za-z0-9_-]{8,100}")


class IdentityError(RuntimeError):
    """A key cannot be registered or used. The message says why and is safe to show."""


def content_digest(body: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii") + ":"


def signature_params(created: int, expires: int, keyid: str, nonce: str) -> str:
    return f'{COVERED};created={created};expires={expires};keyid="{keyid}";nonce="{nonce}";alg="ed25519";tag="agent-escrow"'


def signature_base(method: str, authority: str, path: str, digest: str, params: str) -> bytes:
    lines = [
        f'"@method": {method.upper()}',
        f'"@authority": {authority.lower()}',
        f'"@path": {path.split("?")[0]}',
        f'"content-digest": {digest}',
        f'"@signature-params": {params}',
    ]
    return "\n".join(lines).encode("utf-8")


def sign_request(
    private_pem: bytes,
    keyid: str,
    method: str,
    authority: str,
    path: str,
    body: bytes,
    created: int | None = None,
    nonce: str | None = None,
    lifetime: int = 120,
) -> dict[str, str]:
    """The three headers an agent adds to a request. For agents written in Python, and for tests."""
    serialization, private_cls, _, _ = checkpoint._crypto()
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, private_cls):
        raise IdentityError("the agent key must be an Ed25519 private key")
    created = int(time.time()) if created is None else created
    digest = content_digest(body)
    params = signature_params(created, created + lifetime, keyid, nonce or secrets.token_urlsafe(16))
    signature = key.sign(signature_base(method, authority, path, digest, params))
    return {
        "Content-Digest": digest,
        "Signature-Input": f"{LABEL}={params}",
        "Signature": f"{LABEL}=:" + base64.b64encode(signature).decode("ascii") + ":",
    }


def claimed_keyid(headers: Any) -> str:
    """The key id a request names, cleaned for logs and alerts. Empty when it names none."""
    raw = str(headers.get("signature-input") or "")[:MAX_HEADER_CHARS]
    found = re.search(r';keyid="([^"]*)"', raw)
    keyid = found.group(1) if found else ""
    return keyid if KEYID.fullmatch(keyid) else ""


def verify(method: str, authority: str, path: str, body: bytes, headers: Any, agent_label: str, now: int) -> str | None:
    """None when the request carries a good signature from this agent, or what is wrong.

    Every way of failing returns a reason. Nothing here raises on what an agent sent.
    """
    raw_input = str(headers.get("signature-input") or "")
    raw_signature = str(headers.get("signature") or "")
    if not raw_input and not raw_signature:
        return "the request is not signed"
    if len(raw_input) > MAX_HEADER_CHARS or len(raw_signature) > MAX_HEADER_CHARS:
        return "the signature headers are too long"
    if not raw_input.startswith(f"{LABEL}="):
        return "there is no Signature-Input for sig1"
    params = raw_input[len(LABEL) + 1 :]
    if not params.startswith(COVERED + ";"):
        return "the signature does not cover the method, host, path and body"
    fields = {k: v.strip('"') for k, v in re.findall(r';([a-z]+)=("[^"]*"|\d+)', params)}
    if not {"created", "expires", "keyid", "nonce"} <= set(fields):
        return "the signature needs created, expires, keyid and nonce"
    try:
        created, expires = int(fields["created"]), int(fields["expires"])
    except ValueError:
        return "created and expires must be whole numbers"
    keyid, nonce = fields["keyid"], fields["nonce"]
    if not KEYID.fullmatch(keyid) or not NONCE.fullmatch(nonce):
        return "the key id or the nonce is not in an accepted form"

    row = db.get_agent_key(keyid)
    if row is None:
        return "the signing key is not registered"
    if row.get("revoked_at"):
        return "the signing key has been revoked"
    if row["label"] != agent_label:
        return "the signing key is registered for a different agent"

    if created > now + CLOCK_SKEW_SECONDS:
        return "the signature is dated in the future"
    if expires < now or now - created > MAX_AGE_SECONDS:
        return "the signature has expired"
    digest = str(headers.get("content-digest") or "")
    if digest != content_digest(body):
        return "the body does not match the signed digest"
    match = re.fullmatch(rf"{LABEL}=:([A-Za-z0-9+/=]+):", raw_signature)
    if not match:
        return "there is no Signature for sig1"
    serialization, _, public_cls, invalid = checkpoint._crypto()
    try:
        key = serialization.load_pem_public_key(row["public_key"].encode())
        if not isinstance(key, public_cls):
            return "the registered key is not an Ed25519 key"
        key.verify(
            base64.b64decode(match.group(1), validate=True), signature_base(method, authority, path, digest, params)
        )
    except (invalid, ValueError, TypeError):
        return "the signature does not match the registered key"
    # Last, and only for a signature that is otherwise good: has this exact one been used?
    if not db.use_agent_nonce(keyid, nonce, expires_at=max(expires, created + MAX_AGE_SECONDS) + CLOCK_SKEW_SECONDS):
        return "this signed request has been used before"
    return None


# -- registering keys ------------------------------------------------------------------------


def register(public_pem: str, label: str) -> str:
    """Register a public key for an agent label. Returns its key id."""
    label = label.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        raise IdentityError("the label must be the agent's key label: letters, digits, dot, dash or underscore")
    serialization, _, public_cls, _ = checkpoint._crypto()
    try:
        key = serialization.load_pem_public_key(public_pem.encode())
    except (ValueError, TypeError) as exc:
        raise IdentityError("that is not a public key in PEM form") from exc
    if not isinstance(key, public_cls):
        raise IdentityError("the key must be an Ed25519 public key")
    keyid = checkpoint.fingerprint(public_pem)
    db.ensure_db()
    if not db.add_agent_key(keyid, label, public_pem.strip() + "\n"):
        raise IdentityError(f"that key is already registered (key id {keyid})")
    db.insert_audit_log("agent_key.registered", actor="cli", detail={"keyid": keyid, "label": label})
    return keyid


def revoke(keyid: str, reason: str) -> None:
    db.ensure_db()
    if not db.revoke_agent_key(keyid, reason.strip()[:200]):
        raise IdentityError(f"no key that is still in use has the id {keyid}")
    db.insert_audit_log("agent_key.revoked", actor="cli", detail={"keyid": keyid, "reason": reason.strip()[:200]})


def main(argv: list[str]) -> int:
    usage = (
        "usage: python -m proxy.agent_identity keygen PATH --label LABEL\n"
        "       python -m proxy.agent_identity add PUBLIC_KEY_FILE --label LABEL\n"
        "       python -m proxy.agent_identity list\n"
        "       python -m proxy.agent_identity revoke KEYID --reason TEXT\n"
    )
    try:
        if len(argv) == 4 and argv[0] in ("keygen", "add") and argv[2] == "--label":
            if argv[0] == "keygen":
                path = Path(argv[1])
                checkpoint.keygen(path)
                public_path = path.with_name(path.name + ".pub")
            else:
                public_path = Path(argv[1])
            keyid = register(public_path.read_text(), argv[3])
            if argv[0] == "keygen":
                sys.stdout.write(f"private key  {argv[1]}  (give it to the agent and to nothing else)\n")
            sys.stdout.write(f"registered   {public_path} for agent {argv[3]}\n")
            sys.stdout.write(f"key id       {keyid}\n")
            return 0
        if argv == ["list"]:
            db.ensure_db()
            for row in db.list_agent_keys():
                state = f"revoked {row['revoked_at']}: {row['revoked_reason']}" if row["revoked_at"] else "in use"
                sys.stdout.write(f"{row['keyid']}  {row['label']}  registered {row['created_at']}  {state}\n")
            return 0
        if len(argv) == 4 and argv[0] == "revoke" and argv[2] == "--reason":
            revoke(argv[1], argv[3])
            sys.stdout.write(f"revoked      {argv[1]}\n")
            return 0
    except (IdentityError, checkpoint.CheckpointError, OSError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stderr.write(usage)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
