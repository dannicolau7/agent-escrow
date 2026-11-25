"""Agents sign their requests; the proxy refuses what is unsigned, forged, changed or replayed."""

import asyncio
import json
import time

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from proxy import agent_identity, checkpoint, db, notifications, settings
from proxy.main import app

KEYS = "billing:agent:billing-secret|other:agent:other-secret|rita:approver:rita-secret"
PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
REFUSED, WARNED = "agent.signature_rejected", "agent.signature_warning"


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "signatures.db"
    db._conn = None
    db.init_db()
    monkeypatch.setenv("ESCROW_KEYS", KEYS)
    monkeypatch.setenv("ESCROW_AGENT_SIGNATURES", "required")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    db._conn = None


@pytest.fixture
def agent(tmp_path, client):
    """A key pair registered for the agent whose API key label is "billing"."""
    path = tmp_path / "billing.key"
    checkpoint.keygen(path)
    keyid = agent_identity.register((tmp_path / "billing.key.pub").read_text(), "billing")
    return path.read_bytes(), keyid


def _signed(agent, body=PING, authority="test", path="/mcp", **kw):
    private, keyid = agent
    return agent_identity.sign_request(private, kw.pop("keyid", keyid), "POST", authority, path, body, **kw)


async def _send(client, headers, body=PING, api_key="billing-secret"):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **headers}
    return await client.post("/mcp", content=body, headers=headers)


def _refusals(event=REFUSED):
    return db.get_audit_log(event_type=event)


async def _refused(client, headers, why, body=PING, api_key="billing-secret"):
    r = await _send(client, headers, body, api_key)
    assert r.status_code == 401, r.text
    assert why in r.json()["detail"], r.json()
    rows = _refusals()
    assert rows and why in json.dumps(rows[0]), rows
    return r


async def test_a_signed_request_goes_through(client, agent):
    r = await _send(client, _signed(agent))
    assert r.status_code == 200 and "result" in r.json(), r.text
    assert not _refusals()


async def test_an_unsigned_request_is_refused_and_recorded(client, agent):
    await _refused(client, {}, "not signed")
    row = _refusals()[0]
    assert row["actor"] == "billing"


async def test_a_refused_request_never_reaches_the_queue_or_the_upstream(client, agent, monkeypatch):
    from proxy import forwarder

    called = []
    monkeypatch.setenv("MCP_UPSTREAM_URL", "http://up/mcp")
    forwarder._test_transport = httpx.MockTransport(lambda r: called.append(1) or httpx.Response(200, json={}))
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "delete_user", "arguments": {"id": 1}}}
    ).encode()
    try:
        await _refused(client, {}, "not signed", body=body)
    finally:
        forwarder._test_transport = None
    assert not called
    assert db.get_pending_actions() == []


async def test_a_key_nobody_registered_is_refused(client, agent, tmp_path):
    stranger = tmp_path / "stranger.key"
    checkpoint.keygen(stranger)
    headers = _signed((stranger.read_bytes(), "sha256:" + "0" * 64))
    await _refused(client, headers, "not registered")


async def test_a_forged_signature_under_a_real_key_id_is_refused(client, agent, tmp_path):
    forger = tmp_path / "forger.key"
    checkpoint.keygen(forger)
    headers = _signed((forger.read_bytes(), agent[1]))
    await _refused(client, headers, "does not match the registered key")


async def test_a_body_changed_after_signing_is_refused(client, agent):
    changed = PING.replace(b"ping", b"tools/list")
    await _refused(client, _signed(agent), "does not match the signed digest", body=changed)


async def test_a_digest_swapped_to_fit_a_changed_body_is_refused(client, agent):
    changed = PING.replace(b"ping", b"tools/list")
    headers = _signed(agent)
    headers["Content-Digest"] = agent_identity.content_digest(changed)
    await _refused(client, headers, "does not match the registered key", body=changed)


async def test_the_same_signed_request_works_once(client, agent):
    headers = _signed(agent)
    assert (await _send(client, headers)).status_code == 200
    await _refused(client, headers, "used before")


async def test_two_copies_sent_together_let_one_through(client, agent):
    headers = _signed(agent)
    a, b = await asyncio.gather(_send(client, headers), _send(client, headers))
    assert sorted([a.status_code, b.status_code]) == [200, 401]


async def test_an_old_signature_is_refused(client, agent):
    old = int(time.time()) - 3600
    await _refused(client, _signed(agent, created=old), "expired")


async def test_a_long_lifetime_does_not_outlast_the_limit(client, agent):
    old = int(time.time()) - agent_identity.MAX_AGE_SECONDS - 5
    await _refused(client, _signed(agent, created=old, lifetime=10**6), "expired")


async def test_a_signature_from_the_future_is_refused(client, agent):
    await _refused(client, _signed(agent, created=int(time.time()) + 3600), "future")


async def test_a_signature_for_another_host_or_path_is_refused(client, agent):
    await _refused(client, _signed(agent, authority="elsewhere.example"), "does not match the registered key")
    await _refused(client, _signed(agent, path="/other"), "does not match the registered key")


async def test_the_public_host_setting_is_what_gets_checked(client, agent, monkeypatch):
    monkeypatch.setenv("ESCROW_PUBLIC_AUTHORITY", "Escrow.Example.com")
    assert (await _send(client, _signed(agent, authority="escrow.example.com"))).status_code == 200
    await _refused(client, _signed(agent), "does not match the registered key")


async def test_a_key_works_only_with_its_own_agents_api_key(client, agent):
    """A stolen signing key with another agent's API key, or the reverse, is not enough."""
    await _refused(client, _signed(agent), "different agent", api_key="other-secret")
    assert _refusals()[0]["actor"] == "other"


async def test_a_revoked_key_is_refused(client, agent):
    assert (await _send(client, _signed(agent))).status_code == 200
    agent_identity.revoke(agent[1], "laptop lost")
    await _refused(client, _signed(agent), "revoked")
    assert db.get_audit_log(event_type="agent_key.revoked")


async def test_the_api_key_is_still_needed(client, agent):
    r = await client.post("/mcp", content=PING, headers=_signed(agent))
    assert r.status_code == 401 and "API key" in r.json()["detail"]
    r = await _send(client, _signed(agent), api_key="rita-secret")  # a reviewer is not an agent
    assert r.status_code == 403


@pytest.mark.parametrize(
    "headers, why",
    [
        ({"Signature-Input": "sig1=garbage", "Signature": "sig1=:AAAA:"}, "does not cover"),
        ({"Signature-Input": "other=()", "Signature": "x"}, "no Signature-Input for sig1"),
        ({"Signature-Input": "sig1=" + "a" * 5000, "Signature": "x"}, "too long"),
        ({"Signature": "sig1=:AAAA:"}, "no Signature-Input for sig1"),
        (
            {"Signature-Input": f'sig1={agent_identity.COVERED};created=1;keyid="k"', "Signature": "sig1=:AAAA:"},
            "needs created, expires, keyid and nonce",
        ),
        (
            {
                "Signature-Input": f'sig1={agent_identity.COVERED};created=1;expires=2;keyid="<b>x</b>";nonce="12345678"',
                "Signature": "sig1=:AAAA:",
            },
            "not in an accepted form",
        ),
    ],
)
async def test_nonsense_headers_are_refused_not_crashed_on(client, agent, headers, why):
    await _refused(client, headers, why)


async def test_a_signature_that_is_not_base64_is_refused(client, agent):
    headers = _signed(agent)
    headers["Signature"] = "sig1=:!!!!:"
    await _refused(client, headers, "no Signature for sig1")
    headers = _signed(agent)
    headers["Signature"] = "sig1=:AAA:"
    await _refused(client, headers, "does not match the registered key")


async def test_a_key_id_made_of_markup_is_not_recorded_or_reported(client, agent, monkeypatch):
    sent = []

    async def fake_post(client_, channel, url, payload, action_id):
        sent.append(payload)

    monkeypatch.setattr(notifications, "_post", fake_post)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://slack.test/hook")
    monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "http://hook.test/x")
    headers = {
        "Signature-Input": f'sig1={agent_identity.COVERED};created=1;expires=2;keyid="<!channel>";nonce="12345678"',
        "Signature": "sig1=:AAAA:",
    }
    await _refused(client, headers, "not in an accepted form")
    await asyncio.gather(*list(notifications._tasks))
    assert len(sent) == 2
    assert "<!channel>" not in json.dumps(sent) and "<!channel>" not in json.dumps(_refusals())
    assert "refused" in sent[0]["text"] and "billing" in sent[0]["text"]
    assert sent[1]["event"] == REFUSED and sent[1]["enforced"] is True


async def test_the_alert_does_not_hold_up_the_answer(client, agent, monkeypatch):
    release = asyncio.Event()

    async def slow_post(*a, **k):
        await release.wait()

    monkeypatch.setattr(notifications, "_post", slow_post)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://slack.test/hook")
    r = await asyncio.wait_for(_send(client, {}), timeout=2.0)
    assert r.status_code == 401
    release.set()
    await asyncio.gather(*list(notifications._tasks))


async def test_warn_mode_records_and_lets_the_call_go_on(client, agent, monkeypatch):
    monkeypatch.setenv("ESCROW_AGENT_SIGNATURES", "warn")
    r = await _send(client, {})
    assert r.status_code == 200 and "result" in r.json()
    assert not _refusals()
    row = _refusals(WARNED)[0]
    assert "not signed" in json.dumps(row)
    assert (await _send(client, _signed(agent))).status_code == 200
    assert len(_refusals(WARNED)) == 1


async def test_off_is_the_default_and_ignores_signatures(client, agent, monkeypatch):
    monkeypatch.delenv("ESCROW_AGENT_SIGNATURES")
    assert settings.agent_signature_mode() == "off"
    assert (await _send(client, {})).status_code == 200
    assert (await _send(client, {"Signature-Input": "sig1=garbage", "Signature": "x"})).status_code == 200
    assert not _refusals() and not _refusals(WARNED)


def test_a_misspelt_mode_refuses_to_start_and_fails_closed(monkeypatch):
    monkeypatch.setenv("ESCROW_AGENT_SIGNATURES", "requried")
    with pytest.raises(settings.ConfigError, match="ESCROW_AGENT_SIGNATURES"):
        settings.validate_startup()
    assert settings.agent_signature_mode() == "required"


def test_registering_refuses_what_is_not_an_ed25519_public_key(client, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    with pytest.raises(agent_identity.IdentityError, match="not a public key"):
        agent_identity.register("hello", "billing")
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with pytest.raises(agent_identity.IdentityError, match="Ed25519"):
        agent_identity.register(rsa_pem, "billing")
    path = tmp_path / "k.key"
    checkpoint.keygen(path)
    public = (tmp_path / "k.key.pub").read_text()
    with pytest.raises(agent_identity.IdentityError, match="label"):
        agent_identity.register(public, "two words")
    keyid = agent_identity.register(public, "billing")
    with pytest.raises(agent_identity.IdentityError, match="already registered"):
        agent_identity.register(public, "other")
    assert db.get_agent_key(keyid)["label"] == "billing"
    with pytest.raises(agent_identity.IdentityError, match="no key"):
        agent_identity.revoke("sha256:nope", "x")


def test_the_command_line_registers_lists_and_revokes(client, tmp_path, capsys):
    key = tmp_path / "bot.key"
    assert agent_identity.main(["keygen", str(key), "--label", "billing"]) == 0
    out = capsys.readouterr().out
    keyid = out.split("key id")[1].strip()
    assert key.exists() and db.get_agent_key(keyid)
    assert agent_identity.main(["list"]) == 0
    assert "in use" in capsys.readouterr().out
    assert agent_identity.main(["revoke", keyid, "--reason", "rotated"]) == 0
    assert agent_identity.main(["list"]) == 0
    assert "revoked" in capsys.readouterr().out
    assert agent_identity.main(["revoke", keyid, "--reason", "again"]) == 1
    assert agent_identity.main(["add", str(tmp_path / "missing.pub"), "--label", "x"]) == 1
    assert agent_identity.main(["nonsense"]) == 2
    assert db.get_audit_log(event_type="agent_key.registered")
