from __future__ import annotations

import json
import secrets

from aiohttp.test_utils import TestClient, TestServer

from hermes_feishu_card.runtime_control import (
    RUNTIME_HOOK_GENERATION,
    sign_runtime_request,
)
from hermes_feishu_card.server import create_app


ROOT_SECRET = b"r" * 32


class NeverCalledFeishuClient:
    async def send_card(self, *_args, **_kwargs):
        raise AssertionError("runtime control must not send a card")

    async def update_card_message(self, *_args, **_kwargs):
        raise AssertionError("runtime control must not update a card")


def _payload(**changes):
    payload = {
        "schema_version": "1",
        "event": "runtime.hello",
        "runtime_id": "runtime-1234567890",
        "sequence": 1,
        "created_at": 100.0,
        "hook_generation": RUNTIME_HOOK_GENERATION,
        "package_version": "4.1.0",
    }
    payload.update(changes)
    return payload


def _signed_body(payload):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        **sign_runtime_request(
            ROOT_SECRET,
            body,
            nonce=secrets.token_urlsafe(18),
        ),
    }
    return body, headers


async def test_runtime_endpoint_requires_proof_and_never_echoes_it():
    app = create_app(
        NeverCalledFeishuClient(),
        operations_transport_root_secret=ROOT_SECRET,
        integrity_mode="notify",
        expected_runtime_package_version="4.1.0",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/runtime/events", json=_payload())
        body = await response.json()
        health = await (await client.get("/health")).json()
    finally:
        await client.close()

    assert response.status == 401
    assert body == {"ok": False, "error": "runtime authentication failed"}
    assert "signature" not in json.dumps(body).lower()
    assert health["status"] == "healthy"
    assert health["readiness"]["status"] == "starting"
    assert health["metrics"]["runtime_control_auth_rejections"] == 1


async def test_runtime_endpoint_accepts_signed_hello_and_health_becomes_ready():
    app = create_app(
        NeverCalledFeishuClient(),
        operations_transport_root_secret=ROOT_SECRET,
        integrity_mode="notify",
        expected_runtime_package_version="4.1.0",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    body, headers = _signed_body(_payload())
    try:
        response = await client.post("/runtime/events", data=body, headers=headers)
        response_body = await response.json()
        health = await (await client.get("/health")).json()
        replay = await client.post("/runtime/events", data=body, headers=headers)
    finally:
        await client.close()

    assert response.status == 200
    assert response_body == {"ok": True, "accepted": True}
    assert health["status"] == "healthy"
    assert health["readiness"] == {
        "status": "ready",
        "reason": "runtime_ready",
        "integrity_mode": "notify",
        "runtime_seen": True,
        "generation_match": True,
        "restart_required": False,
        "last_seen_age_seconds": 0,
    }
    assert replay.status == 401


async def test_runtime_generation_mismatch_degrades_readiness_not_liveness():
    app = create_app(
        NeverCalledFeishuClient(),
        operations_transport_root_secret=ROOT_SECRET,
        integrity_mode="safe",
        expected_runtime_package_version="4.1.0",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    body, headers = _signed_body(
        _payload(hook_generation="old-hook", package_version="4.0.21")
    )
    try:
        assert (await client.post("/runtime/events", data=body, headers=headers)).status == 200
        health_response = await client.get("/health")
        health = await health_response.json()
    finally:
        await client.close()

    assert health_response.status == 200
    assert health["status"] == "healthy"
    assert health["readiness"]["status"] == "degraded"
    assert health["readiness"]["reason"] == "gateway_restart_required"
    assert health["readiness"]["restart_required"] is True
    assert "runtime_id" not in json.dumps(health)


async def test_missing_private_root_degrades_readiness_and_refuses_runtime_control():
    app = create_app(
        NeverCalledFeishuClient(),
        operations_transport_root_secret=None,
        integrity_mode="notify",
        expected_runtime_package_version="4.1.0",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/runtime/events", json=_payload())
        health = await (await client.get("/health")).json()
    finally:
        await client.close()

    assert response.status == 503
    assert health["status"] == "healthy"
    assert health["readiness"]["reason"] == "control_auth_unavailable"


async def test_integrity_off_reports_disabled_but_still_authenticates_endpoint():
    app = create_app(
        NeverCalledFeishuClient(),
        operations_transport_root_secret=ROOT_SECRET,
        integrity_mode="off",
        expected_runtime_package_version="4.1.0",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    body, headers = _signed_body(_payload())
    try:
        response = await client.post("/runtime/events", data=body, headers=headers)
        response_body = await response.json()
        health = await (await client.get("/health")).json()
    finally:
        await client.close()

    assert response.status == 200
    assert response_body == {"ok": True, "accepted": False}
    assert health["readiness"]["status"] == "disabled"
