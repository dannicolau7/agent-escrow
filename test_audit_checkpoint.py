"""Signed checkpoints of the audit log."""

import base64
import copy
import stat

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from proxy import checkpoint, db, settings
from proxy.main import app


@pytest_asyncio.fixture
async def client(tmp_path):
    db.DB_PATH = tmp_path / "checkpoint.db"
    db._conn = None
    db.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    db._conn = None


@pytest.fixture
def key(tmp_path, monkeypatch):
    path = tmp_path / "audit.key"
    fp = checkpoint.keygen(path)
    monkeypatch.setenv("ESCROW_AUDIT_SIGNING_KEY", str(path))
    return path, (tmp_path / "audit.key.pub").read_text(), fp


async def test_checkpoint_signs_the_head_and_verifies_with_the_public_key(client, key):
    _, public_pem, fp = key
    for i in range(3):
        db.insert_audit_log("test.event", detail={"i": i})
    cp = (await client.get("/api/audit/checkpoint")).json()

    head = db.audit_head()
    assert (cp["last_id"], cp["hash"], cp["rows"]) == (head["last_id"], head["hash"], head["rows"])
    assert cp["format"] == checkpoint.FORMAT and cp["keyed_chain"] is False
    assert cp["public_key_fingerprint"] == fp and cp["public_key"] == public_pem
    assert checkpoint.verify(cp, public_pem)
    # The checkpoint covers the record of its own issue, and the chain still verifies.
    newest = db.get_audit_log(limit=1)[0]
    assert newest["event_type"] == "audit.checkpoint_issued" and newest["hash"] == cp["hash"]
    assert (await client.get("/api/audit/verify")).json()["ok"] is True


async def test_any_change_to_a_checkpoint_breaks_it(client, key, tmp_path):
    _, public_pem, _ = key
    db.insert_audit_log("test.event")
    cp = (await client.get("/api/audit/checkpoint")).json()
    for field, value in [
        ("hash", "0" * 64),
        ("last_id", cp["last_id"] + 1),
        ("rows", cp["rows"] - 1),
        ("signed_at", "2020-01-01T00:00:00+00:00"),
        ("keyed_chain", True),
        ("format", "something-else/1"),
        ("signature", base64.b64encode(b"x" * 64).decode()),
        ("signature", "not base64!"),
    ]:
        forged = copy.deepcopy(cp)
        forged[field] = value
        assert not checkpoint.verify(forged, public_pem), field
    missing = {k: v for k, v in cp.items() if k != "signature"}
    assert not checkpoint.verify(missing, public_pem)


async def test_a_checkpoint_signed_by_another_key_is_rejected(client, key, tmp_path, monkeypatch):
    _, public_pem, _ = key
    db.insert_audit_log("test.event")
    other = tmp_path / "other.key"
    checkpoint.keygen(other)
    monkeypatch.setenv("ESCROW_AUDIT_SIGNING_KEY", str(other))
    forged = (await client.get("/api/audit/checkpoint")).json()
    # It carries its own (attacker's) public key, which must never be what gets trusted.
    assert checkpoint.verify(forged, forged["public_key"])
    assert not checkpoint.verify(forged, public_pem)
    assert not checkpoint.verify(forged, "not a key")


async def test_rewriting_history_after_a_checkpoint_is_detectable(client, key):
    _, public_pem, _ = key
    db.insert_audit_log("action.rejected", action_id="act_1", actor="rita")
    cp = (await client.get("/api/audit/checkpoint")).json()

    # Someone with database access turns the rejection into an approval and rebuilds the
    # whole (unkeyed) chain so that it verifies again.
    conn = db.get_conn()
    conn.execute("UPDATE audit_log SET event_type='action.approved' WHERE action_id='act_1'")
    prev = ""
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall():
        digest = db._audit_digest(prev, dict(row))
        conn.execute("UPDATE audit_log SET prev_hash=?, hash=? WHERE id=?", (prev, digest, row["id"]))
        prev = digest
    conn.commit()
    assert (await client.get("/api/audit/verify")).json()["ok"] is True  # the chain alone is fooled

    # The checkpoint that already left the building is not: its row now has another hash.
    row = conn.execute("SELECT hash FROM audit_log WHERE id=?", (cp["last_id"],)).fetchone()
    assert checkpoint.verify(cp, public_pem) and row["hash"] != cp["hash"]


async def test_only_an_admin_can_ask_for_a_checkpoint(client, key, monkeypatch):
    monkeypatch.setenv("ESCROW_KEYS", "v:viewer:kv|p:approver:kp|g:agent:kg|a:admin:ka")
    assert (await client.get("/api/audit/checkpoint")).status_code == 401
    for k in ("kv", "kp", "kg", "wrong"):
        r = await client.get("/api/audit/checkpoint", headers={"X-Api-Key": k})
        assert r.status_code in (401, 403), k
    before = db.audit_head()["rows"]
    ok = await client.get("/api/audit/checkpoint", headers={"X-Api-Key": "ka"})
    assert ok.status_code == 200 and db.audit_head()["rows"] == before + 1
    assert db.get_audit_log(limit=1)[0]["actor"] == "a"


async def test_no_key_configured_means_no_checkpoint_and_nothing_logged(client, monkeypatch):
    monkeypatch.delenv("ESCROW_AUDIT_SIGNING_KEY", raising=False)
    db.insert_audit_log("test.event")
    before = db.audit_head()
    r = await client.get("/api/audit/checkpoint")
    assert r.status_code == 409 and "ESCROW_AUDIT_SIGNING_KEY" in r.json()["detail"]
    assert db.audit_head() == before


async def test_private_key_never_leaves_and_bad_keys_stop_startup(client, key, tmp_path, monkeypatch):
    path, _, _ = key
    private = path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    db.insert_audit_log("test.event")
    r = await client.get("/api/audit/checkpoint")
    secret_body = "".join(private.strip().splitlines()[1:-1])
    assert "PRIVATE KEY" not in r.text and secret_body not in r.text.replace("\\n", "")

    with pytest.raises(checkpoint.CheckpointError, match="refusing to overwrite"):
        checkpoint.keygen(path)

    settings.validate_startup()  # a good key is fine
    bad = tmp_path / "bad.key"
    bad.write_text("-----BEGIN PRIVATE KEY-----\nSECRETSECRET\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("ESCROW_AUDIT_SIGNING_KEY", str(bad))
    with pytest.raises(settings.ConfigError) as excinfo:
        settings.validate_startup()
    assert "SECRETSECRET" not in str(excinfo.value)
    assert (await client.get("/api/audit/checkpoint")).status_code == 500
    monkeypatch.setenv("ESCROW_AUDIT_SIGNING_KEY", str(tmp_path / "missing.key"))
    with pytest.raises(settings.ConfigError):
        settings.validate_startup()


async def test_keyed_chain_is_flagged_in_the_checkpoint(client, key, monkeypatch):
    monkeypatch.setenv("ESCROW_AUDIT_KEY", "chain-secret")
    db.insert_audit_log("test.event")
    cp = (await client.get("/api/audit/checkpoint")).json()
    assert cp["keyed_chain"] is True and checkpoint.verify(cp, key[1])


def test_keygen_command(tmp_path, capsys):
    target = tmp_path / "k"
    assert checkpoint.main(["keygen", str(target)]) == 0
    out = capsys.readouterr().out
    assert "fingerprint" in out and "PRIVATE" not in out
    assert checkpoint.main(["keygen", str(target)]) == 1
    assert checkpoint.main([]) == 2
