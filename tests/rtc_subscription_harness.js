"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const caseName = process.argv[2];

function createElement() {
  return {
    className: "",
    disabled: false,
    hidden: false,
    children: [],
    textContent: "",
    value: "",
    addEventListener() {},
    replaceChildren() {
      this.children = [];
    },
  };
}

function createDocument() {
  const elements = new Map();
  return {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, createElement());
      }
      return elements.get(id);
    },
  };
}

function createDingRtc({ audioFailures = 0, screenFailures = 0 } = {}) {
  const subscribed = [];
  const published = [];
  const unpublished = [];
  const listeners = new Map();
  let audioAttempts = 0;
  let screenAttempts = 0;
  const track = {
    trackMediaType: "video",
    closed: false,
    stopped: false,
    on() {},
    async play() {},
    close() {
      this.closed = true;
    },
    stop() {
      this.stopped = true;
    },
  };

  const client = {
    remoteUsers: [],
    on(eventName, listener) {
      listeners.set(eventName, listener);
    },
    emit(eventName, ...args) {
      const listener = listeners.get(eventName);
      if (listener) {
        listener(...args);
      }
    },
    async subscribe(userId, mediaType, auxiliary) {
      subscribed.push({ userId, mediaType, auxiliary });
      if (mediaType === "audio" && userId === "mcu") {
        audioAttempts += 1;
        if (audioAttempts <= audioFailures) {
          const error = new Error("audio stream not ready");
          error.code = 16974851;
          throw error;
        }
      }
      if (mediaType === "video" && auxiliary) {
        screenAttempts += 1;
        if (screenAttempts <= screenFailures) {
          const error = new Error("screen stream not ready");
          error.code = 16974851;
          throw error;
        }
      }
      // DingRTC Web SDK 3.9.36 only accepts audio subscriptions for "mcu".
      if (mediaType === "audio" && userId !== "mcu") {
        const error = new Error("only support subscribe mcu audio");
        error.code = 16974083;
        error.name = "InvaildArgument";
        throw error;
      }
      return track;
    },
    async publish(tracks) {
      published.push(...(Array.isArray(tracks) ? tracks : [tracks]));
    },
    async unpublish(tracks) {
      unpublished.push(...(Array.isArray(tracks) ? tracks : [tracks]));
    },
    async leave() {},
  };

  return {
    sdk: {
      default: {
        createClient() {
          return client;
        },
        async createScreenVideoTrack() {
          return [track];
        },
      },
    },
    client,
    subscribed,
    published,
    unpublished,
    track,
  };
}

function loadCallerSandbox(dingRtc) {
  const document = createDocument();
  const emittedErrors = [];
  const fetchMock = async (url) => {
    if (url === "/config.json") {
      return {
        ok: true,
        async json() {
          return {
            backendUrl: "http://127.0.0.1:8000",
            clientId: "caller-test",
            defaultName: "Caller",
          };
        },
      };
    }
    return {
      ok: true,
      async json() {
        return { rtcConfigured: true };
      },
    };
  };

  class WebSocketMock {
    static OPEN = 1;
  }

  return {
    sandbox: {
      clearInterval,
      clearTimeout,
      console,
      document,
      fetch: fetchMock,
      process,
      setInterval,
      setTimeout,
      WebSocket: WebSocketMock,
      window: {
        DingRTC: dingRtc.sdk,
        addEventListener() {},
      },
    },
    emittedErrors,
  };
}

function loadAgentSandbox(dingRtc) {
  const document = createDocument();
  const emittedEvents = [];
  const bridge = {
    emitEvent(raw) {
      emittedEvents.push(JSON.parse(raw));
    },
  };

  class QWebChannelMock {
    constructor(_transport, callback) {
      callback({ objects: { qtBridge: bridge } });
    }
  }

  return {
    sandbox: {
      clearInterval,
      clearTimeout,
      console,
      document,
      process,
      QWebChannel: QWebChannelMock,
      qt: { webChannelTransport: {} },
      setInterval: (callback, delay) => {
        const timer = setInterval(callback, Math.min(delay, 50));
        timer.unref();
        return timer;
      },
      setTimeout,
      window: {
        DingRTC: dingRtc.sdk,
      },
    },
    emittedEvents,
  };
}

function loadCallerRegistrationSandbox() {
  const document = createDocument();
  const savedCredentials = [];
  let registrationCount = 0;
  const nativeSetTimeout = setTimeout;
  return {
    sandbox: {
      clearInterval,
      clearTimeout,
      console,
      document,
      async fetch(url, options = {}) {
        url = String(url);
        if (url === "/config.json") {
          return {
            ok: true,
            async json() {
              return {
                backendUrl: "http://127.0.0.1:8000",
                defaultName: "注册测试客户端",
                installationUuid: "11111111-1111-4111-8111-111111111111",
                clientCredential: null,
              };
            },
          };
        }
        if (url.endsWith("/api/health")) {
          return {
            ok: true,
            async json() {
              return { ok: true, rtcConfigured: true };
            },
          };
        }
        if (url === "/client-credential") {
          savedCredentials.push(JSON.parse(options.body).client_credential);
          return {
            ok: true,
            async json() {
              return { ok: true };
            },
          };
        }
        if (url.endsWith("/api/clients/register")) {
          registrationCount += 1;
          return {
            ok: true,
            async json() {
              return {
                client: {
                  id: "client-registration-test",
                  displayName: "注册测试客户端",
                  status: registrationCount === 1 ? "pending" : "approved",
                },
                credential: registrationCount === 1 ? "credential-1" : null,
              };
            },
          };
        }
        throw new Error(`unexpected fetch: ${url}`);
      },
      process,
      setInterval,
      setTimeout: (callback, delay, ...args) =>
        nativeSetTimeout(callback, Math.min(delay, 10), ...args),
      testDelay: (delay) =>
        new Promise((resolve) => nativeSetTimeout(resolve, delay)),
      WebSocket: class {
        static OPEN = 1;
      },
      window: {
        DingRTC: { default: {} },
        addEventListener() {},
      },
    },
    savedCredentials,
  };
}

function loadCallerRegistrationRecoverySandbox() {
  const document = createDocument();
  document.getElementById("reRegister").hidden = true;
  const savedCredentials = [];
  const state = {
    registrationCount: 0,
    resetCount: 0,
  };
  const nativeSetTimeout = setTimeout;
  return {
    sandbox: {
      clearInterval,
      clearTimeout,
      console,
      document,
      async fetch(url, options = {}) {
        url = String(url);
        if (url === "/config.json") {
          return {
            ok: true,
            status: 200,
            async json() {
              return {
                backendUrl: "http://127.0.0.1:8000",
                defaultName: "失效凭证客户端",
                installationUuid: "11111111-1111-4111-8111-111111111111",
                clientCredential: "invalid-credential",
              };
            },
          };
        }
        if (url.endsWith("/api/health")) {
          return {
            ok: true,
            status: 200,
            async json() {
              return { ok: true, rtcConfigured: true };
            },
          };
        }
        if (url.endsWith("/api/clients/register")) {
          state.registrationCount += 1;
          if (state.registrationCount === 1) {
            return {
              ok: false,
              status: 403,
              async json() {
                return { detail: "客户端设备凭证无效" };
              },
            };
          }
          return {
            ok: true,
            status: 200,
            async json() {
              return {
                client: {
                  id: "client-after-reset",
                  displayName: "失效凭证客户端",
                  status: "pending",
                },
                credential: "credential-after-reset",
              };
            },
          };
        }
        if (url === "/client-reset-identity") {
          state.resetCount += 1;
          return {
            ok: true,
            status: 200,
            async json() {
              return {
                installationUuid: "22222222-2222-4222-8222-222222222222",
                clientCredential: null,
              };
            },
          };
        }
        if (url === "/client-credential") {
          savedCredentials.push(JSON.parse(options.body).client_credential);
          return {
            ok: true,
            status: 200,
            async json() {
              return { ok: true };
            },
          };
        }
        throw new Error(`unexpected fetch: ${url}`);
      },
      process,
      setInterval,
      setTimeout: (callback, delay, ...args) => {
        const timer = nativeSetTimeout(callback, delay, ...args);
        timer.unref();
        return timer;
      },
      testDelay: (delay) =>
        new Promise((resolve) => nativeSetTimeout(resolve, delay)),
      WebSocket: class {
        static OPEN = 1;
      },
      window: {
        DingRTC: { default: {} },
        addEventListener() {},
      },
    },
    savedCredentials,
    state,
  };
}

async function run() {
  if (
    caseName !== "caller" &&
    caseName !== "caller-registration" &&
    caseName !== "caller-registration-recovery" &&
    caseName !== "agent" &&
    caseName !== "caller-screen" &&
    caseName !== "caller-audio-retry" &&
    caseName !== "agent-audio-retry" &&
    caseName !== "agent-screen-retry" &&
    caseName !== "agent-screen-discovery"
  ) {
    throw new Error(
      "usage: node rtc_subscription_harness.js caller|caller-registration|caller-registration-recovery|agent|caller-screen|caller-audio-retry|agent-audio-retry|agent-screen-retry|agent-screen-discovery",
    );
  }

  const dingRtc = createDingRtc({
    audioFailures: caseName.includes("audio-retry") ? 3 : 0,
    screenFailures: caseName === "agent-screen-retry" ? 3 : 0,
  });
  const sourcePath =
    caseName.startsWith("agent")
      ? path.join(ROOT, "agent", "rtc", "rtc.js")
      : path.join(ROOT, "caller", "web", "app.js");
  const loaded =
    caseName.startsWith("agent")
      ? loadAgentSandbox(dingRtc)
      : caseName === "caller-registration"
        ? loadCallerRegistrationSandbox()
        : caseName === "caller-registration-recovery"
          ? loadCallerRegistrationRecoverySandbox()
        : loadCallerSandbox(dingRtc);
  loaded.sandbox.subscriptionCalls = dingRtc.subscribed;
  loaded.sandbox.publishedTracks = dingRtc.published;
  loaded.sandbox.unpublishedTracks = dingRtc.unpublished;
  if (loaded.savedCredentials) {
    loaded.sandbox.savedCredentials = loaded.savedCredentials;
  }
  if (loaded.state) {
    loaded.sandbox.recoveryState = loaded.state;
  }
  const context = vm.createContext(loaded.sandbox);
  const source = fs.readFileSync(sourcePath, "utf8");
  const assertion =
    caseName === "caller-registration"
      ? `
    ;(async () => {
      try {
        await testDelay(100);
        if (
          document.getElementById("clientApproval").textContent !== "已批准" ||
          document.getElementById("clientId").value !== "client-registration-test" ||
          document.getElementById("startCall").disabled ||
          document.getElementById("displayName").value !== "注册测试客户端" ||
          !document.getElementById("displayName").readOnly ||
          !document.getElementById("reRegister").hidden
        ) {
          throw new Error(
            "caller registration did not reach approved state: " +
            JSON.stringify({
              approval: document.getElementById("clientApproval").textContent,
              clientId: document.getElementById("clientId").value,
              startDisabled: document.getElementById("startCall").disabled,
              displayName: document.getElementById("displayName").value,
              displayNameReadOnly:
                document.getElementById("displayName").readOnly,
              errorText: document.getElementById("errorText").textContent,
            }),
          );
        }
        if (savedCredentials[0] !== "credential-1") {
          throw new Error("client credential was not persisted");
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `
      : caseName === "caller-registration-recovery"
        ? `
    ;(async () => {
      try {
        await testDelay(50);
        const recoveryButton = document.getElementById("reRegister");
        if (
          recoveryButton.hidden ||
          recoveryButton.textContent !== "重新注册" ||
          document.getElementById("clientApproval").textContent !== "注册失败"
        ) {
          throw new Error(
            "invalid credential did not expose registration recovery: " +
            JSON.stringify({
              hidden: recoveryButton.hidden,
              text: recoveryButton.textContent,
              approval: document.getElementById("clientApproval").textContent,
              errorText: document.getElementById("errorText").textContent,
            }),
          );
        }
        await reregisterClient();
        await testDelay(50);
        if (
          recoveryState.resetCount !== 1 ||
          recoveryState.registrationCount !== 2 ||
          document.getElementById("clientApproval").textContent !==
            "等待管理员审批" ||
          document.getElementById("clientId").value !== "client-after-reset" ||
          savedCredentials[0] !== "credential-after-reset"
        ) {
          throw new Error(
            "registration recovery did not issue a new pending identity: " +
            JSON.stringify({
              resetCount: recoveryState.resetCount,
              registrationCount: recoveryState.registrationCount,
              approval: document.getElementById("clientApproval").textContent,
              clientId: document.getElementById("clientId").value,
              savedCredentials,
              errorText: document.getElementById("errorText").textContent,
            }),
          );
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `
      : caseName === "caller-screen"
      ? `
    ;(async () => {
      try {
        call = { id: "call-screen", status: "active" };
        configureRtcClient();
        await startScreenShare();
        if (
          !screenTrack ||
          publishedTracks.length !== 1 ||
          publishedTracks[0] !== screenTrack
        ) {
          throw new Error("expected one published screen track");
        }
        await stopScreenShare(true);
        if (
          screenTrack !== null ||
          unpublishedTracks.length !== 1 ||
          unpublishedTracks[0] !== publishedTracks[0] ||
          !publishedTracks[0].closed
        ) {
          throw new Error("screen track was not stopped cleanly");
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `
      : caseName === "agent-screen-retry"
        ? `
    ;(async () => {
      try {
        screenRetryDelayMs = 10;
        buildClient();
        client.remoteUsers.push({
          userId: "caller-01",
          hasAuxiliary: true,
        });
        client.emit(
          "user-published",
          { userId: "caller-01" },
          "video",
          true,
        );
        await new Promise((resolve) => setTimeout(resolve, 1800));
        if (
          !remoteTracks.has("screen:caller-01") ||
          screenRetryTimer !== null ||
          document.getElementById("screenState").textContent !== "正在播放"
        ) {
          throw new Error(
            "screen subscription did not recover after retry, calls=" +
            JSON.stringify(subscriptionCalls),
          );
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `
        : caseName === "agent-screen-discovery"
          ? `
    ;(async () => {
      try {
        buildClient();
        client.remoteUsers.push({
          userId: "caller-discovery",
          hasAuxiliary: true,
        });
        await new Promise((resolve) => setTimeout(resolve, 180));
        if (
          !remoteTracks.has("screen:caller-discovery") ||
          document.getElementById("screenState").textContent !== "正在播放"
        ) {
          throw new Error(
            "screen discovery did not subscribe without user-published event",
          );
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `
        : caseName.endsWith("audio-retry")
          ? `
    ;(async () => {
      try {
        audioRetryDelayMs = 10;
        ${caseName.startsWith("agent") ? "buildClient();" : "configureRtcClient();"}
        client.emit("user-published", { userId: "remote-01" }, "audio");
        await new Promise((resolve) => setTimeout(resolve, 1800));
        if (
          !remoteTracks.has("mcu") ||
          audioRetryTimer !== null ||
          subscriptionCalls.length !== 4
        ) {
          throw new Error(
            "audio subscription did not recover after retry, calls=" +
            JSON.stringify(subscriptionCalls),
          );
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `
      : `
    ;(async () => {
      try {
        ${caseName === "caller" ? "configureRtcClient();" : "buildClient();"}
        client.emit("user-published", { userId: "agent-01" }, "audio");
        client.emit("user-published", { userId: "agent-01" }, "audio");
        await new Promise((resolve) => setTimeout(resolve, 30));
        ${
          caseName === "agent"
            ? `
        client.emit(
          "user-published",
          { userId: "caller-01" },
          "video",
          true,
        );
        await new Promise((resolve) => setTimeout(resolve, 30));
        `
            : ""
        }
        const subscribedToMcu = subscriptionCalls
          .some((call) => call.userId === "mcu" && call.mediaType === "audio");
        const expectedCount = ${caseName === "agent" ? 2 : 1};
        const subscribedToScreen = subscriptionCalls
          .some((call) =>
            call.userId === "caller-01" &&
            call.mediaType === "video" &&
            call.auxiliary === true
          );
        if (
          !subscribedToMcu ||
          subscriptionCalls.length !== expectedCount ||
          remoteTracks.size !== expectedCount ||
          (${caseName === "agent" ? "!subscribedToScreen" : "false"})
        ) {
          throw new Error("unexpected subscription state, got " +
            JSON.stringify(subscriptionCalls) +
            ", tracks=" + remoteTracks.size);
        }
      } catch (error) {
        console.error(error.stack || error.message || String(error));
        process.exitCode = 1;
      }
    })();
  `;
  vm.runInContext(`${source}\n${assertion}`, context, {
    filename: sourcePath,
  });
}

run().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exitCode = 1;
});
