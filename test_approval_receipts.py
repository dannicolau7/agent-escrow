"""Signed receipts for approved actions."""

import base64
import copy
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from proxy import checkpoint, db, receipt
from proxy.main import app

ARGS = {"to": "ACME-0042", "amount": 250.0, "memo": "invoice 17"}


@pytest_asyncio.fixture
async def client(tmp_path):
    db.DB_PATH = tmp_path / "receipts.db"
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


def _hold(action_id="act_1", args=ARGS, submitted_by="billing-agent"):
    db.insert_action(
        action_id=action_id,
        rpc_id="1",
        tool_name="send_payment",
        category="mutation",
        risk_level="HIGH",
        reason="moves money",
        args=args,
        submitted_by=submitted_by,
    )
    return action_id


def _approve(action_id="act_1", by="rita"):
    assert db.update_action_status(action_id, "APPROVED", resolved_by=by)


async def test_a_receipt_names_the_reviewer_and_the_exact_call(client, key):
    _, public_pem, fp = key
    _approve(_hold())
    r = await client.get("/api/actions/act_1/receipt")
    assert r.status_code == 200
    signed = r.json()
    assert signed["format"] == receipt.FORMAT and signed["decision"] == "APPROVED"
    assert (signed["tool_name"], signed["risk_level"]) == ("send_payment", "HIGH")
    assert (signed["submitted_by"], signed["approved_by"]) == ("billing-agent", "rita")
    assert signed["arguments_sha256"] == receipt.arguments_digest(ARGS)
    assert signed["public_key_fingerprint"] == fp
    assert signed["approved_at"] and signed["requested_at"] and signed["issued_at"]
    # The arguments themselves are never in the receipt.
    assert "ACME-0042" not in json.dumps(signed) and "raw_arguments" not in signed

    assert receipt.problems(signed, public_pem) == []
    assert receipt.problems(signed, public_pem, ARGS) == []
    # Key order does not matter; any change to the call does.
    assert receipt.problems(signed, public_pem, dict(reversed(list(ARGS.items())))) == []
    for changed in ({**ARGS, "amount": 2500.0}, {**ARGS, "to": "MALLORY-1"}, {}, None, [ARGS]):
        assert receipt.problems(signed, public_pem, changed) == ["these are not the arguments that were approved"]

    # Issuing one is written to the audit log, without the arguments.
    newest = db.get_audit_log(limit=1)[0]
    assert (newest["event_type"], newest["action_id"]) == ("action.receipt_issued", "act_1")
    assert "ACME-0042" not in json.dumps(newest)
    assert db.verify_audit_chain()["ok"] is True


async def test_only_an_action_a_reviewer_approved_has_a_receipt(client, key):
    before = db.audit_head()["rows"]
    assert (await client.get("/api/actions/nope/receipt")).status_code == 404

    _hold("pending")
    _hold("rejected")
    db.update_action_status("rejected", "REJECTED", "no", resolved_by="rita")
    _hold("timed_out")
    db.update_action_status("timed_out", "REJECTED", "Hold timed out", resolved_by="system")
    for action_id in ("pending", "rejected", "timed_out"):
        r = await client.get(f"/api/actions/{action_id}/receipt")
        assert r.status_code == 409 and "approved" in r.json()["detail"], action_id

    # Rows that say APPROVED but name no person: never trusted, whatever wrote them.
    for who in ("system", "unknown", "", None):
        action_id = _hold(f"odd-{who}")
        conn = db.get_conn()
        conn.execute(
            "UPDATE pending_actions SET status='APPROVED', resolved_by=?, resolved_at='2026-01-01' WHERE id=?",
            (who, action_id),
        )
        conn.commit()
        r = await client.get(f"/api/actions/{action_id}/receipt")
        assert r.status_code == 409 and "no recorded reviewer" in r.json()["detail"], who
    assert db.audit_head()["rows"] == before  # nothing refused is recorded as issued


async def test_any_change_to_a_receipt_breaks_it(client, key):
    _, public_pem, _ = key
    _approve(_hold())
    signed = (await client.get("/api/actions/act_1/receipt")).json()
    for field, value in [
        ("action_id", "act_2"),
        ("tool_name", "delete_everything"),
        ("arguments_sha256", "0" * 64),
        ("risk_level", "LOW"),
        ("approved_by", "mallory"),
        ("submitted_by", "other-agent"),
        ("approved_at", "2020-01-01T00:00:00+00:00"),
        ("requested_at", "2020-01-01T00:00:00+00:00"),
        ("issued_at", "2020-01-01T00:00:00+00:00"),
        ("decision", "REJECTED"),
        ("signature", base64.b64encode(b"x" * 64).decode()),
        ("signature", "not base64!"),
    ]:
        forged = copy.deepcopy(signed)
        forged[field] = value
        assert receipt.problems(forged, public_pem), field
    assert receipt.problems({k: v for k, v in signed.items() if k != "signature"}, public_pem)
    assert receipt.problems({**signed, "format": checkpoint.FORMAT}, public_pem) == ["this is not an approval receipt"]
    for junk in (None, "receipt", [], {}):
        assert receipt.problems(junk, public_pem) == ["this is not an approval receipt"]


async def test_a_receipt_from_another_key_and_a_checkpoint_are_both_rejected(client, key, tmp_path, monkeypatch):
    _, public_pem, _ = key
    _approve(_hold())
    # A checkpoint is signed by the same key but is not a receipt, and the reverse.
    cp = (await client.get("/api/audit/checkpoint")).json()
    assert receipt.problems(cp, public_pem) == ["this is not an approval receipt"]
    signed = (await client.get("/api/actions/act_1/receipt")).json()
    assert not checkpoint.verify(signed, public_pem)

    other = tmp_path / "other.key"
    checkpoint.keygen(other)
    monkeypatch.setenv("ESCROW_AUDIT_SIGNING_KEY", str(other))
    forged = (await client.get("/api/actions/act_1/receipt")).json()
    assert receipt.problems(forged, forged["public_key"]) == []  # its own key vouches for it
    assert "signature does not match" in receipt.problems(forged, public_pem)[0]
    assert receipt.problems(forged, "not a key") == ["the public key cannot be read"]


async def test_who_may_ask_for_a_receipt(client, key, monkeypatch):
    _approve(_hold())
    monkeypatch.setenv("ESCROW_KEYS", "v:viewer:kv|p:approver:kp|g:agent:kg|a:admin:ka")
    assert (await client.get("/api/actions/act_1/receipt")).status_code == 401
    for k in ("kg", "wrong"):
        r = await client.get("/api/actions/act_1/receipt", headers={"X-Api-Key": k})
        assert r.status_code == 403, k
    before = db.audit_head()["rows"]
    for k, label in (("kv", "v"), ("kp", "p"), ("ka", "a")):
        r = await client.get("/api/actions/act_1/receipt", headers={"X-Api-Key": k})
        assert r.status_code == 200, k
        assert db.get_audit_log(limit=1)[0]["actor"] == label
    assert db.audit_head()["rows"] == before + 3
    # A key in the URL is not accepted here.
    assert (await client.get("/api/actions/act_1/receipt?api_key=ka")).status_code == 401


async def test_no_key_or_a_broken_key_means_no_receipt_and_nothing_logged(client, tmp_path, monkeypatch):
    _approve(_hold())
    before = db.audit_head()["rows"]
    monkeypatch.delenv("ESCROW_AUDIT_SIGNING_KEY", raising=False)
    r = await client.get("/api/actions/act_1/receipt")
    assert r.status_code == 409 and "ESCROW_AUDIT_SIGNING_KEY" in r.json()["detail"]

    bad = tmp_path / "bad.key"
    bad.write_text("-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("ESCROW_AUDIT_SIGNING_KEY", str(bad))
    r = await client.get("/api/actions/act_1/receipt")
    assert r.status_code == 500 and "secret-material" not in r.text
    assert db.audit_head()["rows"] == before

    with pytest.raises(receipt.ReceiptError):
        receipt.issue({"id": "x", "status": "APPROVED", "resolved_by": "rita", "resolved_at": "t"})


async def test_command_line_check(client, key, tmp_path, capsys):
    path, public_pem, _ = key
    _approve(_hold())
    signed = (await client.get("/api/actions/act_1/receipt")).json()
    files = {name: tmp_path / name for name in ("receipt.json", "args.json", "other.json", "bad.json")}
    files["receipt.json"].write_text(json.dumps(signed))
    files["args.json"].write_text(json.dumps(ARGS))
    files["other.json"].write_text(json.dumps({**ARGS, "amount": 9999}))
    files["bad.json"].write_text(json.dumps({**signed, "approved_by": "mallory"}))
    pub = f"{path}.pub"

    assert receipt.main(["verify", str(files["receipt.json"]), "--pubkey", pub]) == 0
    out = capsys.readouterr().out
    assert "rita approved send_payment" in out and "arguments were not checked" in out
    assert (
        receipt.main(["verify", str(files["receipt.json"]), "--pubkey", pub, "--arguments", str(files["args.json"])])
        == 0
    )
    assert "arguments match" in capsys.readouterr().out
    assert (
        receipt.main(["verify", str(files["receipt.json"]), "--pubkey", pub, "--arguments", str(files["other.json"])])
        == 1
    )
    assert "not the arguments that were approved" in capsys.readouterr().out
    assert receipt.main(["verify", str(files["bad.json"]), "--pubkey", pub]) == 1
    assert "RECEIPT NOT VALID" in capsys.readouterr().out
    assert receipt.main(["verify", str(tmp_path / "missing.json"), "--pubkey", pub]) == 2
    assert receipt.main(["verify", str(files["receipt.json"])]) == 2
    assert receipt.main(["issue"]) == 2
