from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from server.app import (
    START_ACTION,
    STOP_ACTION,
    AIGCApp,
    RuntimeConfig,
    build_start_request,
    create_app,
    create_openapi_invoker,
    create_runtime_scenes,
    expand_env_references,
    load_runtime_config,
)
from server.signer import sign_headers
from server.token import AccessToken, Privileges

APP_ID = "012345678901234567890123"
APP_KEY = "01234567890123456789012345678901"


def scene_definitions():
    return {
        "default": {
            "SceneConfig": {"name": "Default"},
            "VoiceChat": {
                "Config": {
                    "ASRConfig": {"Provider": "volcano", "ProviderParams": {}},
                    "LLMConfig": {"Mode": "ArkV3"},
                    "TTSConfig": {"Provider": "volcano", "ProviderParams": {}},
                },
                "AgentConfig": {"UserId": "voice_agent", "EnableConversationStateCallback": False},
            },
        },
        "alternate": {
            "SceneConfig": {"name": "Alternate"},
            "VoiceChat": {
                "Config": {
                    "ASRConfig": {"Provider": "volcano", "ProviderParams": {}},
                    "LLMConfig": {"Mode": "ArkV3"},
                    "TTSConfig": {"Provider": "volcano", "ProviderParams": {}},
                },
                "AgentConfig": {"UserId": "alternate_agent"},
            },
        },
    }


def runtime_config(**overrides):
    values = dict(
        app_id=APP_ID,
        app_key=APP_KEY,
        business_id=None,
        host="127.0.0.1",
        port=3001,
        env={},
        access_key_id="ak",
        secret_key="sk",
    )
    values.update(overrides)
    return RuntimeConfig(**values)


def test_runtime_config_validation():
    base = {"RTC_APP_ID": APP_ID, "RTC_APP_KEY": APP_KEY}
    env = {**base, "VOLCENGINE_ACCESS_KEY_ID": " ak ", "VOLCENGINE_SECRET_ACCESS_KEY": " sk "}
    config = load_runtime_config(env)
    assert (config.access_key_id, config.secret_key) == ("ak", "sk")
    with pytest.raises(ValueError, match="VOLCENGINE_ACCESS_KEY_ID"):
        load_runtime_config(base)
    with pytest.raises(ValueError, match="必须同时配置"):
        load_runtime_config({**base, "VOLCENGINE_ACCESS_KEY_ID": "ak"})


def test_runtime_scene_and_business_id():
    definitions = scene_definitions()
    config = runtime_config(business_id="demo_business")
    scenes = create_runtime_scenes(definitions, config)
    request = build_start_request(scenes["default"], config)
    assert scenes["default"]["rtc"]["BusinessId"] == "demo_business"
    assert request["BusinessId"] == "demo_business"
    assert request["AgentConfig"]["TargetUserId"] == [scenes["default"]["rtc"]["UserId"]]
    assert request["AgentConfig"]["EnableConversationStateCallback"] is True
    assert definitions["default"]["VoiceChat"]["AgentConfig"]["EnableConversationStateCallback"] is False
    assert APP_KEY not in json.dumps(scenes["default"]["rtc"])


def test_env_reference_expansion():
    env = {"API_SECRET": "secret"}
    assert expand_env_references({"a": "${API_SECRET}", "b": "x-${API_SECRET}"}, env) == {
        "a": "secret",
        "b": "x-${API_SECRET}",
    }
    with pytest.raises(ValueError, match="缺少环境变量 MISSING"):
        expand_env_references("${MISSING}", env)


def test_signer_matches_node_implementation():
    headers = sign_headers(
        method="POST",
        region="cn-north-1",
        service="rtc",
        params={"Action": START_ACTION, "Version": "2025-06-01"},
        headers={"Host": "rtc.volcengineapi.com", "Content-type": "application/json"},
        body='{"AppId":"app"}',
        access_key_id="ak",
        secret_key="sk",
        now=datetime(2026, 9, 25, 2, 39, 46, tzinfo=timezone.utc),
    )
    assert headers["Authorization"] == (
        "HMAC-SHA256 Credential=ak/20260925/cn-north-1/rtc/request, "
        "SignedHeaders=host;x-content-sha256;x-date, "
        "Signature=0e9b6f782f31f3bc5e3d1145897318961a2f0bd88dd830476e1370314ffa86c1"
    )


def test_rtc_token_binary_format_matches_node_implementation():
    token = AccessToken(APP_ID, APP_KEY, "room", "user")
    token.nonce = 0x12345678
    token.issued_at = 1700000000
    token.expire_at = 1700086400
    token.add_privilege(Privileges.PrivSubscribeStream, 0)
    token.add_privilege(Privileges.PrivPublishStream, 0)
    assert token.serialize() == (
        "001012345678901234567890123OAB4VjQSAPFTZYBCVWUEAHJvb20EAHVzZXIF"
        "AAAAAAAAAAEAAAAAAAIAAAAAAAMAAAAAAAQAAAAAACAAL31V1l7sqYS4RwRosmuF"
        "j3TdHNRamEKelu7oHS/dToc="
    )


@pytest.mark.asyncio
async def test_openapi_invoker_contract():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ResponseMetadata": {}, "Result": "ok"})

    transport = httpx.MockTransport(handler)

    class Client(httpx.AsyncClient):
        def __init__(self):
            super().__init__(transport=transport)

    invoke = create_openapi_invoker(
        runtime_config(),
        client_factory=Client,
        sign_now=lambda: datetime(2026, 9, 25, 2, 39, 46, tzinfo=timezone.utc),
    )
    await invoke(START_ACTION, {"AppId": APP_ID, "RoomId": "room", "TaskId": "task"})
    request = requests[-1]
    assert request.url.params["Action"] == START_ACTION
    assert request.url.params["Version"] == "2025-06-01"
    assert request.headers["authorization"].startswith("HMAC-SHA256 ")


@pytest.mark.asyncio
async def test_http_surface_and_session_isolation():
    calls = []

    async def invoke_voice_chat(action, body):
        calls.append({"action": action, "body": body})
        return {"ResponseMetadata": {"Action": action}, "Result": "ok"}

    wrapped = create_app(
        config=runtime_config(),
        scene_definitions=scene_definitions(),
        invoke_voice_chat=invoke_voice_chat,
    )
    inner: AIGCApp = wrapped.app
    transport = httpx.ASGITransport(app=wrapped)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health", headers={"Origin": "http://local-web.example"})
        assert health.json() == {"ok": True}
        assert health.headers["access-control-allow-origin"] == "*"
        assert (await client.get("/sts")).status_code == 404

        first = (await client.post("/getScenes", json={})).json()
        second = (await client.post("/getScenes", json={})).json()
        assert first["Result"]["SessionID"] != second["Result"]["SessionID"]
        assert first["Result"]["scenes"][0]["rtc"]["RoomId"] != second["Result"]["scenes"][0]["rtc"]["RoomId"]

        session_id = first["Result"]["SessionID"]
        started = await client.post(
            "/proxy",
            params={"Action": START_ACTION},
            json={"SessionID": session_id, "SceneID": "default"},
        )
        assert started.json()["Result"] == "ok"

        stopped = await client.post(
            "/proxy",
            params={"Action": STOP_ACTION},
            content=f"SessionID={session_id}&SceneID=default",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert stopped.json()["Result"] == "ok"

        switched = await client.post(
            "/proxy",
            params={"Action": START_ACTION},
            json={"SessionID": session_id, "SceneID": "alternate"},
        )
        assert switched.json()["Result"] == "ok"

        rejected = await client.post(
            "/proxy",
            params={"Action": "DescribeInstances"},
            json={"SessionID": session_id, "SceneID": "default"},
        )
        assert rejected.json()["ResponseMetadata"]["Error"]

    await inner.stop_active_agents()
    assert any(call["action"] == STOP_ACTION for call in calls)
