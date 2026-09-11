const assert = require("node:assert/strict");
const test = require("node:test");

const {
  START_ACTION,
  STOP_ACTION,
  buildStartRequest,
  createApp,
  createRuntimeScenes,
  createOpenApiInvoker,
  loadRuntimeConfig,
} = require("./app");

const appId = "012345678901234567890123";
const appKey = "01234567890123456789012345678901";

function sceneDefinitions() {
  return {
    default: {
      SceneConfig: { name: "Default" },
      VoiceChat: {
        Config: {
          ASRConfig: { Provider: "volcano", ProviderParams: {} },
          LLMConfig: { Mode: "ArkV3" },
          TTSConfig: { Provider: "volcano", ProviderParams: {} },
        },
        AgentConfig: {
          UserId: "voice_agent",
          EnableConversationStateCallback: false,
        },
      },
    },
    alternate: {
      SceneConfig: { name: "Alternate" },
      VoiceChat: {
        Config: {
          ASRConfig: { Provider: "volcano", ProviderParams: {} },
          LLMConfig: { Mode: "ArkV3" },
          TTSConfig: { Provider: "volcano", ProviderParams: {} },
        },
        AgentConfig: { UserId: "alternate_agent" },
      },
    },
  };
}

function runtimeConfig(overrides = {}) {
  return {
    appId,
    appKey,
    host: "127.0.0.1",
    port: 3001,
    endpoint: "https://rtc.volcengineapi.com",
    version: "2025-06-01",
    region: "cn-north-1",
    service: "rtc",
    env: {},
    credential: { accessKeyId: "ak", secretKey: "sk" },
    ...overrides,
  };
}

test("loadRuntimeConfig requires complete credentials and selects the API version", () => {
  const base = { RTC_APP_ID: appId, RTC_APP_KEY: appKey };
  const env = {
    ...base,
    VOLCENGINE_ACCESS_KEY_ID: " ak ",
    VOLCENGINE_SECRET_ACCESS_KEY: " sk ",
  };
  const config = loadRuntimeConfig(env);
  assert.deepEqual(config.credential, { accessKeyId: "ak", secretKey: "sk" });
  assert.equal(config.version, "2025-06-01");
  assert.equal(
    loadRuntimeConfig({ ...env, VOLCENGINE_RTC_API_VERSION: "2024-12-01" }).version,
    "2024-12-01"
  );
  assert.throws(() => loadRuntimeConfig(base), /VOLCENGINE_ACCESS_KEY_ID/);
  for (const partial of [
    { VOLCENGINE_ACCESS_KEY_ID: "ak" },
    { VOLCENGINE_SECRET_ACCESS_KEY: "sk" },
  ]) {
    assert.throws(() => loadRuntimeConfig({ ...base, ...partial }), /必须同时配置/);
  }
});

test("loadRuntimeConfig reads the optional BusinessId", () => {
  const env = {
    RTC_APP_ID: appId,
    RTC_APP_KEY: appKey,
    VOLCENGINE_ACCESS_KEY_ID: "ak",
    VOLCENGINE_SECRET_ACCESS_KEY: "sk",
  };

  assert.equal(
    loadRuntimeConfig({ ...env, RTC_BUSINESS_ID: "demo_business" }).businessId,
    "demo_business"
  );
  assert.equal(loadRuntimeConfig(env).businessId, undefined);
});

test("runtime propagates the optional BusinessId", () => {
  const config = runtimeConfig({ businessId: "demo_business" });
  const scenes = createRuntimeScenes(sceneDefinitions(), config);

  assert.equal(scenes.default.rtc.BusinessId, "demo_business");
  assert.equal(buildStartRequest(scenes.default, config).BusinessId, "demo_business");

  const configWithoutBusinessId = runtimeConfig();
  const scenesWithoutBusinessId = createRuntimeScenes(
    sceneDefinitions(),
    configWithoutBusinessId
  );
  assert.equal(
    buildStartRequest(scenesWithoutBusinessId.default, configWithoutBusinessId)
      .BusinessId,
    undefined
  );
});

test("scene runtime injects session identities without mutating the scene file", () => {
  const definitions = sceneDefinitions();
  const scenes = createRuntimeScenes(definitions, runtimeConfig());
  const request = buildStartRequest(scenes.default, runtimeConfig());

  assert.equal(request.AppId, appId);
  assert.equal(request.RoomId, scenes.default.rtc.RoomId);
  assert.equal(request.TaskId, scenes.default.taskId);
  assert.equal(request.AgentConfig.UserId, "voice_agent");
  assert.equal(scenes.default.scene.botName, "voice_agent");
  assert.deepEqual(request.AgentConfig.TargetUserId, [
    scenes.default.rtc.UserId,
  ]);
  assert.equal(request.AgentConfig.EnableConversationStateCallback, true);
  assert.equal(
    definitions.default.VoiceChat.AgentConfig.EnableConversationStateCallback,
    false
  );
  assert.equal(definitions.default.VoiceChat.AppId, undefined);
  assert.equal(
    definitions.default.VoiceChat.AgentConfig.TargetUserId,
    undefined
  );
  assert.equal(JSON.stringify(scenes.default.rtc).includes(appKey), false);
});

test("scene requires a persisted agent user id", () => {
  const definitions = sceneDefinitions();
  delete definitions.default.VoiceChat.AgentConfig.UserId;
  assert.throws(
    () => createRuntimeScenes(definitions, runtimeConfig()),
    /VoiceChat\.AgentConfig\.UserId 不能为空/
  );
});

test("OpenAPI invoker signs start and stop with the configured version", async () => {
  const requests = [];
  const config = loadRuntimeConfig({
    RTC_APP_ID: appId,
    RTC_APP_KEY: appKey,
    VOLCENGINE_ACCESS_KEY_ID: "ak",
    VOLCENGINE_SECRET_ACCESS_KEY: "sk",
    VOLCENGINE_RTC_API_VERSION: "2025-06-01",
  });
  const invoke = createOpenApiInvoker(config, async (url, options) => {
    requests.push({ url: new URL(url), options });
    return { json: async () => ({ ResponseMetadata: {}, Result: "ok" }) };
  });
  const body = { AppId: appId, RoomId: "room", TaskId: "task" };
  for (const action of [START_ACTION, STOP_ACTION]) {
    await invoke(action, body);
    const request = requests.at(-1);
    assert.equal(request.url.searchParams.get("Action"), action);
    assert.equal(request.url.searchParams.get("Version"), "2025-06-01");
    assert.match(request.options.headers.Authorization, /^HMAC-SHA256 /);
    assert.deepEqual(JSON.parse(request.options.body), body);
  }
});

test("runtime preserves avatar presentation and VoiceChat configuration", () => {
  const definitions = sceneDefinitions();
  const avatar = { Enabled: true, BackgroundUrl: "https://example.com/avatar.png" };
  definitions.default.VoiceChat.Config.AvatarConfig = avatar;
  const config = runtimeConfig();
  const scenes = createRuntimeScenes(definitions, config);
  assert.equal(scenes.default.scene.isAvatarScene, true);
  assert.equal(scenes.default.scene.avatarBgUrl, avatar.BackgroundUrl);
  assert.deepEqual(buildStartRequest(scenes.default, config).Config.AvatarConfig, avatar);
});

test("HTTP surface isolates sessions and allows concurrent pages", async (t) => {
  const calls = [];
  const app = createApp({
    config: runtimeConfig(),
    sceneDefinitions: sceneDefinitions(),
    invokeVoiceChat: async (action, body) => {
      calls.push({ action, body });
      return { ResponseMetadata: { Action: action }, Result: "ok" };
    },
  });
  const server = app.listen(0, "127.0.0.1");
  t.after(() => new Promise((resolve) => server.close(resolve)));
  await new Promise((resolve) => server.once("listening", resolve));
  const { port } = server.address();
  const origin = `http://127.0.0.1:${port}`;

  const healthResponse = await fetch(`${origin}/health`, {
    headers: { Origin: "http://local-web.example" },
  });
  assert.equal(healthResponse.headers.get("access-control-allow-origin"), "*");
  const health = await healthResponse.json();
  assert.deepEqual(health, { ok: true });
  assert.equal((await fetch(`${origin}/sts`)).status, 404);

  const createSession = () =>
    fetch(`${origin}/getScenes`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    }).then((response) => response.json());
  const invoke = (Action, SessionID, SceneID = "default") =>
    fetch(`${origin}/proxy?Action=${Action}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ SessionID, SceneID }),
    }).then((response) => response.json());

  const [firstPage, secondPage, thirdPage] = await Promise.all([
    createSession(),
    createSession(),
    createSession(),
  ]);
  for (const response of [firstPage, secondPage, thirdPage]) {
    assert.equal(response.Result.scenes[0].scene.id, "default");
    assert.equal("AppKey" in response.Result.scenes[0].rtc, false);
  }
  assert.equal(
    new Set(
      [firstPage, secondPage, thirdPage].map(
        (response) => response.Result.SessionID
      )
    ).size,
    3
  );
  assert.equal(
    new Set(
      [firstPage, secondPage, thirdPage].map(
        (response) => response.Result.scenes[0].rtc.RoomId
      )
    ).size,
    3
  );

  const starts = await Promise.all([
    invoke(START_ACTION, firstPage.Result.SessionID),
    invoke(START_ACTION, secondPage.Result.SessionID),
    invoke(START_ACTION, thirdPage.Result.SessionID),
  ]);
  assert.ok(starts.every(({ Result }) => Result === "ok"));

  assert.equal(
    (
      await fetch(`${origin}/proxy?Action=${STOP_ACTION}`, {
        method: "POST",
        body: new URLSearchParams({
          SessionID: firstPage.Result.SessionID,
          SceneID: "default",
        }),
      }).then((response) => response.json())
    ).Result,
    "ok"
  );
  assert.equal(
    (await invoke(START_ACTION, firstPage.Result.SessionID, "alternate"))
      .Result,
    "ok"
  );
  const startCalls = calls.filter(({ action }) => action === START_ACTION);
  assert.equal(startCalls.length, 4);
  assert.equal(new Set(startCalls.map(({ body }) => body.RoomId)).size, 4);
  assert.equal(new Set(startCalls.map(({ body }) => body.TaskId)).size, 4);
  assert.deepEqual(
    new Set(startCalls.map(({ body }) => body.AgentConfig.UserId)),
    new Set(["voice_agent", "alternate_agent"])
  );
  assert.ok(
    startCalls.every(({ body }) => body.AgentConfig.TargetUserId.length === 1)
  );
  assert.ok(
    startCalls.every(
      ({ body }) => body.AgentConfig.EnableConversationStateCallback === true
    )
  );

  const rejected = await fetch(`${origin}/proxy?Action=DescribeInstances`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      SessionID: thirdPage.Result.SessionID,
      SceneID: "default",
    }),
  }).then((response) => response.json());
  assert.ok(rejected.ResponseMetadata.Error);

  await app.stopActiveAgents();
  assert.equal(calls.filter(({ action }) => action === STOP_ACTION).length, 4);
});
