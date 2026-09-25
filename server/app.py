# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs

import httpx
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .signer import compact_json, sign_headers
from .token import AccessToken, Privileges
from .util import assert_value, read_files, wrapper

START_ACTION = "StartVoiceChat"
STOP_ACTION = "StopVoiceChat"
ALLOWED_ACTIONS = {START_ACTION, STOP_ACTION}
RTC_API_VERSION = "2025-06-01"
RTC_ENDPOINT = "https://rtc.volcengineapi.com"
RTC_REGION = "cn-north-1"
RTC_SERVICE = "rtc"
SERVER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVER_DIR.parent

load_dotenv(PROJECT_DIR / ".env.local", override=False)


def required(value: str | None, name: str) -> str:
    normalized = value.strip() if value is not None else ""
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


@dataclass(frozen=True)
class RuntimeConfig:
    app_id: str
    app_key: str
    business_id: str | None
    host: str
    port: int
    env: Mapping[str, str]
    access_key_id: str
    secret_key: str


def _parse_port(value: str | None) -> int:
    try:
        number = float(value or "3001")
    except (TypeError, ValueError):
        raise ValueError("PORT 必须为正整数") from None
    if not math.isfinite(number) or not number.is_integer() or number <= 0:
        raise ValueError("PORT 必须为正整数")
    return int(number)


def load_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    env = os.environ if env is None else env
    access_key_id = (env.get("VOLCENGINE_ACCESS_KEY_ID") or "").strip()
    secret_key = (env.get("VOLCENGINE_SECRET_ACCESS_KEY") or "").strip()
    if bool(access_key_id) != bool(secret_key):
        raise ValueError("VOLCENGINE_ACCESS_KEY_ID 和 VOLCENGINE_SECRET_ACCESS_KEY 必须同时配置")
    return RuntimeConfig(
        app_id=required(env.get("RTC_APP_ID"), "RTC_APP_ID"),
        app_key=required(env.get("RTC_APP_KEY"), "RTC_APP_KEY"),
        business_id=(env.get("RTC_BUSINESS_ID") or "").strip() or None,
        host=(env.get("HOST") or "").strip() or "127.0.0.1",
        port=_parse_port(env.get("PORT")),
        env=env,
        access_key_id=required(access_key_id, "VOLCENGINE_ACCESS_KEY_ID"),
        secret_key=required(secret_key, "VOLCENGINE_SECRET_ACCESS_KEY"),
    )


def deep_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _js_boolean(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return bool(value)
    return True


def _strict_number_equals(value: Any, expected: int) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == expected


def validate_scene(scene_id: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{scene_id}: scene 必须为对象")
    if not isinstance(value.get("SceneConfig"), dict):
        raise ValueError(f"{scene_id}: SceneConfig 不能为空")
    voice_chat = value.get("VoiceChat")
    if not isinstance(voice_chat, dict) or not voice_chat.get("Config") or not voice_chat.get("AgentConfig"):
        raise ValueError(f"{scene_id}: VoiceChat.Config 和 VoiceChat.AgentConfig 不能为空")
    user_id = voice_chat["AgentConfig"].get("UserId")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError(f"{scene_id}: VoiceChat.AgentConfig.UserId 不能为空")


def create_rtc_token(config: RuntimeConfig, room_id: str, user_id: str) -> str:
    token = AccessToken(config.app_id, config.app_key, room_id, user_id)
    token.add_privilege(Privileges.PrivSubscribeStream, 0)
    token.add_privilege(Privileges.PrivPublishStream, 0)
    token.expire_time(int(time.time()) + 24 * 3600)
    return token.serialize()


def create_runtime_scenes(scene_definitions: Mapping[str, Any], config: RuntimeConfig) -> dict[str, dict[str, Any]]:
    result = {}
    for scene_id, definition in scene_definitions.items():
        validate_scene(scene_id, definition)
        room_id, user_id, task_id = (str(uuid.uuid4()) for _ in range(3))
        voice_chat = deep_clone(definition["VoiceChat"])
        voice_config = voice_chat["Config"]
        agent_config = voice_chat["AgentConfig"]
        avatar = voice_config.get("AvatarConfig") or {}
        vision = (voice_config.get("LLMConfig") or {}).get("VisionConfig") or {}
        snapshot = vision.get("SnapshotConfig") or {}
        scene = {
            "id": scene_id,
            **deep_clone(definition["SceneConfig"]),
            "botName": agent_config["UserId"],
            "isAvatarScene": _js_boolean(avatar.get("Enabled")),
            "isInterruptMode": _strict_number_equals(voice_config.get("InterruptMode"), 0),
            "isVision": _js_boolean(vision.get("Enable")),
            "isScreenMode": _strict_number_equals(snapshot.get("StreamType"), 1),
        }
        if avatar.get("BackgroundUrl") is not None:
            scene["avatarBgUrl"] = avatar["BackgroundUrl"]
        rtc = {
            "AppId": config.app_id,
            "RoomId": room_id,
            "UserId": user_id,
            "Token": create_rtc_token(config, room_id, user_id),
        }
        if config.business_id is not None:
            rtc["BusinessId"] = config.business_id
        result[scene_id] = {
            "scene": scene,
            "rtc": rtc,
            "voiceChat": voice_chat,
            "roomId": room_id,
            "userId": user_id,
            "taskId": task_id,
            "status": "idle",
            "pending": None,
        }
    return result


_ENV_REFERENCE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


def expand_env_references(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [expand_env_references(item, env) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_references(item, env) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    match = _ENV_REFERENCE.fullmatch(value)
    if not match:
        return value
    key = match.group(1)
    if key not in env:
        raise ValueError(f"缺少环境变量 {key}")
    return env[key]


def build_start_request(runtime_scene: Mapping[str, Any], config: RuntimeConfig) -> dict[str, Any]:
    voice_chat = expand_env_references(deep_clone(runtime_scene["voiceChat"]), config.env)
    result = {
        **voice_chat,
        "AppId": config.app_id,
        "RoomId": runtime_scene["roomId"],
        "TaskId": runtime_scene["taskId"],
        "AgentConfig": {
            **voice_chat["AgentConfig"],
            "TargetUserId": [runtime_scene["userId"]],
            "EnableConversationStateCallback": True,
        },
    }
    if config.business_id is not None:
        result["BusinessId"] = config.business_id
    return result


def build_stop_request(runtime_scene: Mapping[str, Any], config: RuntimeConfig) -> dict[str, Any]:
    return {"AppId": config.app_id, "RoomId": runtime_scene["roomId"], "TaskId": runtime_scene["taskId"]}


VoiceChatInvoker = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def create_openapi_invoker(config: RuntimeConfig, client_factory=None, sign_now=None) -> VoiceChatInvoker:
    client_factory = client_factory or (lambda: httpx.AsyncClient())

    async def invoke(action: str, body: dict[str, Any]) -> dict[str, Any]:
        body_text = compact_json(body)
        headers = sign_headers(
            method="POST",
            region=RTC_REGION,
            service=RTC_SERVICE,
            params={"Action": action, "Version": RTC_API_VERSION},
            headers={"Host": "rtc.volcengineapi.com", "Content-type": "application/json"},
            body=body_text,
            access_key_id=config.access_key_id,
            secret_key=config.secret_key,
            now=sign_now() if sign_now else None,
        )
        async with client_factory() as client:
            response = await client.post(
                RTC_ENDPOINT,
                params={"Action": action, "Version": RTC_API_VERSION},
                headers=headers,
                content=body_text.encode(),
            )
            return response.json()

    return invoke


def response_succeeded(response: Mapping[str, Any] | None) -> bool:
    return not (response and isinstance(response.get("ResponseMetadata"), Mapping) and response["ResponseMetadata"].get("Error"))


def local_success(action: str) -> dict[str, Any]:
    return {"ResponseMetadata": {"Action": action, "RequestId": f"local-{uuid.uuid4()}"}, "Result": "ok"}


async def _parse_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    if "application/x-www-form-urlencoded" in content_type:
        values = parse_qs(raw.decode(), keep_blank_values=True)
        return {key: value[-1] if value else "" for key, value in values.items()}
    return {}


class AIGCApp(Starlette):
    def __init__(self, *, config: RuntimeConfig, scene_definitions: Mapping[str, Any], invoke_voice_chat: VoiceChatInvoker) -> None:
        self.config = config
        self.scene_definitions = scene_definitions
        self.invoke_voice_chat = invoke_voice_chat
        self.sessions: dict[str, dict[str, Any]] = {}
        @asynccontextmanager
        async def lifespan(_: Starlette):
            yield
            await self.stop_active_agents()

        super().__init__(
            routes=[Route("/{path:path}", self._handle_request, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])],
            lifespan=lifespan,
        )

    def _create_session(self) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        session = {"id": session_id, "activeSceneId": None, "scenes": create_runtime_scenes(self.scene_definitions, self.config)}
        self.sessions[session_id] = session
        return session

    @staticmethod
    def _release(session, runtime_scene) -> None:
        runtime_scene["status"] = "idle"
        if session["activeSceneId"] == runtime_scene["scene"]["id"]:
            session["activeSceneId"] = None

    async def _start(self, session, runtime_scene):
        assert_value(not session["activeSceneId"] or session["activeSceneId"] == runtime_scene["scene"]["id"], "同一 session 只允许一个 scene 进行通话")
        if runtime_scene["status"] == "starting":
            return await runtime_scene["pending"]
        if runtime_scene["status"] == "stopping":
            await runtime_scene["pending"]
            return await self._start(session, runtime_scene)
        if runtime_scene["status"] == "active":
            return local_success(START_ACTION)
        session["activeSceneId"] = runtime_scene["scene"]["id"]
        runtime_scene["status"] = "starting"
        runtime_scene["pending"] = asyncio.create_task(self.invoke_voice_chat(START_ACTION, build_start_request(runtime_scene, self.config)))
        try:
            response = await runtime_scene["pending"]
            if response_succeeded(response):
                runtime_scene["status"] = "active"
            else:
                self._release(session, runtime_scene)
            return response
        except Exception:
            self._release(session, runtime_scene)
            raise
        finally:
            runtime_scene["pending"] = None

    async def _stop(self, session, runtime_scene):
        if runtime_scene["status"] == "idle":
            return local_success(STOP_ACTION)
        if runtime_scene["status"] == "starting":
            try:
                await runtime_scene["pending"]
            except Exception:
                return local_success(STOP_ACTION)
            return await self._stop(session, runtime_scene)
        if runtime_scene["status"] == "stopping":
            await runtime_scene["pending"]
            return local_success(STOP_ACTION)
        previous_status = runtime_scene["status"]
        runtime_scene["status"] = "stopping"
        runtime_scene["pending"] = asyncio.create_task(self.invoke_voice_chat(STOP_ACTION, build_stop_request(runtime_scene, self.config)))
        try:
            response = await runtime_scene["pending"]
            if response_succeeded(response):
                self._release(session, runtime_scene)
            else:
                runtime_scene["status"] = previous_status
            return response
        except Exception:
            runtime_scene["status"] = previous_status
            raise
        finally:
            runtime_scene["pending"] = None

    async def _proxy_logic(self, request: Request):
        action = request.query_params.get("Action")
        assert_value(action in ALLOWED_ACTIONS, "Action 仅支持 StartVoiceChat 或 StopVoiceChat")
        body = await _parse_body(request)
        session_id, scene_id = body.get("SessionID"), body.get("SceneID")
        assert_value(session_id, "SessionID 不能为空")
        assert_value(scene_id, "SceneID 不能为空")
        session = self.sessions.get(session_id)
        assert_value(session, f"{session_id} 不存在")
        runtime_scene = session["scenes"].get(scene_id)
        assert_value(runtime_scene, f"{scene_id} 不存在")
        return await (self._start(session, runtime_scene) if action == START_ACTION else self._stop(session, runtime_scene))

    def _get_scenes_logic(self):
        session = self._create_session()
        return {
            "SessionID": session["id"],
            "scenes": [{"scene": item["scene"], "rtc": item["rtc"]} for item in session["scenes"].values()],
        }

    async def _handle_request(self, request: Request) -> Response:
        if request.method == "GET" and request.url.path == "/health":
            return JSONResponse({"ok": True})
        matched, payload = await wrapper(
            method=request.method,
            path=request.url.path,
            api_name="proxy",
            contain_response_metadata=False,
            logic=lambda: self._proxy_logic(request),
        )
        if matched:
            return JSONResponse(payload)
        matched, payload = await wrapper(
            method=request.method,
            path=request.url.path,
            api_name="getScenes",
            logic=self._get_scenes_logic,
        )
        if matched:
            return JSONResponse(payload)
        return Response(status_code=404)

    async def stop_active_agents(self) -> None:
        active = [(session, scene) for session in self.sessions.values() for scene in session["scenes"].values() if scene["status"] != "idle"]

        async def stop_one(session, scene):
            if scene["status"] in {"starting", "stopping"} and scene["pending"] is not None:
                try:
                    await scene["pending"]
                except Exception:
                    pass
            if scene["status"] == "active":
                await self._stop(session, scene)

        await asyncio.gather(*(stop_one(session, scene) for session, scene in active), return_exceptions=True)
        for session, scene in active:
            if scene["status"] != "idle":
                self._release(session, scene)


def create_app(*, config=None, env=None, scene_definitions=None, invoke_voice_chat=None) -> Starlette:
    config = config or load_runtime_config(env)
    definitions = scene_definitions if scene_definitions is not None else read_files(SERVER_DIR / "scenes", ".json")
    app = AIGCApp(
        config=config,
        scene_definitions=definitions,
        invoke_voice_chat=invoke_voice_chat or create_openapi_invoker(config),
    )
    return CORSMiddleware(app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _main() -> None:
    parser = argparse.ArgumentParser(description="RTC AIGC Python server")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    config = load_runtime_config()
    uvicorn.run(
        "server.app:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload=args.reload,
        reload_dirs=[str(SERVER_DIR)] if args.reload else None,
        reload_includes=["*.py", "*.json"] if args.reload else None,
    )


if __name__ == "__main__":
    _main()
