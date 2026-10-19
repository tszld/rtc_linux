"use strict";

const sdk = window.DingRTC.default || window.DingRTC;
const $ = (id) => document.getElementById(id);

let config;
let client;
let microphone;
let call;
let callerToken;
let ws;
let reconnectTimer;
let heartbeatTimer;
let muted = false;
let ending = false;
let remoteTracks = new Map();
let audioSubscriptionPromise = null;
let audioRetryTimer = null;
let audioRetryDelayMs = 1000;
let audioRetryErrorReported = false;
let screenTrack = null;
let screenPublishing = false;

const MICROPHONE_TIMEOUT_MS = 30000;
const RTC_JOIN_TIMEOUT_MS = 20000;
const RTC_PUBLISH_TIMEOUT_MS = 15000;
const MCU_AUDIO_UID = "mcu";
const AUDIO_RETRY_MAX_MS = 5000;
const END_REASON_LABELS = {
  caller_hangup: "呼叫方已挂断",
  agent_hangup: "客服已挂断",
  agent_force_end: "客服已结束会话",
  interrupted_timeout: "连接中断超时",
  caller_join_timeout: "呼叫端入会超时",
  backend_restart: "控制服务已重启",
};

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function errorMessage(error, fallback) {
  return error?.message || error?.code || error?.name || fallback;
}

async function checkMicrophonePermission() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("当前浏览器不支持麦克风采集。");
  }
  if (!navigator.permissions?.query) {
    return;
  }
  let permission;
  try {
    permission = await navigator.permissions.query({ name: "microphone" });
  } catch (_) {
    return;
  }
  if (permission.state === "denied") {
    throw new Error("浏览器已拒绝麦克风权限，请在地址栏允许后重试。");
  }
}

function setError(message) {
  $("errorText").textContent = message || "";
}

function setBadge(status) {
  const labels = {
    created: "正在准备",
    waiting: "等待客服",
    connecting: "客服连接中",
    active: "通话中",
    interrupted: "连接异常",
    ended: "已结束",
  };
  $("stateBadge").textContent = labels[status] || "未呼叫";
  $("stateBadge").className = `badge ${status || "idle"}`;
  $("sessionStatus").textContent = labels[status] || "-";
}

function setBusy(busy) {
  $("startCall").disabled = busy;
  $("hangup").disabled = !busy;
  $("mute").disabled = !busy || !microphone;
  updateScreenShareButton();
}

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `HTTP_${response.status}`);
  }
  return body;
}

async function checkBackend() {
  try {
    const response = await fetch(`${config.backendUrl}/api/health`);
    const health = await readJson(response);
    $("connection").textContent = health.rtcConfigured
      ? "后端已连接"
      : "后端已连接，RTC 未配置";
    $("connection").className = health.rtcConfigured
      ? "status-dot ok"
      : "status-dot bad";
  } catch (error) {
    $("connection").textContent = "无法连接后端";
    $("connection").className = "status-dot bad";
    setError(`集中后端不可达：${config.backendUrl}`);
  }
}

function stopRemoteTracks() {
  for (const track of remoteTracks.values()) {
    try {
      track.stop();
    } catch (_) {}
  }
  remoteTracks.clear();
  $("remoteAudio").textContent = "未连接";
}

function updateScreenShareButton() {
  const isSharing = Boolean(screenTrack);
  $("shareScreen").textContent = isSharing ? "停止共享" : "共享屏幕";
  $("shareScreen").disabled =
    !call || !client || ending || screenPublishing;
  $("screenShareState").textContent = screenPublishing
    ? "正在启动"
    : isSharing
      ? "共享中"
      : "未共享";
}

async function stopScreenShare(unpublish = true) {
  if (!screenTrack || screenPublishing) {
    return;
  }
  const track = screenTrack;
  screenPublishing = true;
  updateScreenShareButton();
  try {
    if (unpublish && client) {
      await withTimeout(
        client.unpublish([track]),
        RTC_PUBLISH_TIMEOUT_MS,
        "停止共享屏幕超时。",
      );
    }
  } catch (error) {
    setError(`停止共享屏幕失败：${errorMessage(error, "未知错误")}`);
  } finally {
    screenTrack = null;
    screenPublishing = false;
    try {
      track.close();
    } catch (_) {}
    updateScreenShareButton();
  }
}

async function startScreenShare() {
  if (
    !client ||
    !call ||
    ending ||
    screenTrack ||
    screenPublishing
  ) {
    return;
  }
  setError("");
  screenPublishing = true;
  updateScreenShareButton();
  let track;
  try {
    const tracks = await withTimeout(
      sdk.createScreenVideoTrack({
        displaySurface: "monitor",
        systemAudio: "exclude",
      }),
      MICROPHONE_TIMEOUT_MS,
      "等待选择共享屏幕超时。",
    );
    track =
      tracks.find((item) => item.trackMediaType === "video") ||
      tracks[0];
    if (!track) {
      throw new Error("未获取到屏幕视频轨道。");
    }
    if (ending || !client) {
      track.close();
      return;
    }
    track.on("track-ended", () => {
      stopScreenShare(true);
    });
    await withTimeout(
      client.publish([track]),
      RTC_PUBLISH_TIMEOUT_MS,
      "发布屏幕画面超时。",
    );
    screenTrack = track;
    $("statusText").textContent = "屏幕共享已开始，客服可以查看当前画面。";
  } catch (error) {
    if (track && track !== screenTrack) {
      try {
        track.close();
      } catch (_) {}
    }
    const name = error?.name || "";
    if (name === "NotAllowedError" || name === "AbortError") {
      setError("已取消屏幕共享。");
    } else {
      setError(`共享屏幕失败：${errorMessage(error, "未知错误")}`);
    }
  } finally {
    screenPublishing = false;
    updateScreenShareButton();
  }
}

function stopRemoteAudio() {
  clearAudioRetry();
  const track = remoteTracks.get(MCU_AUDIO_UID);
  if (track) {
    try {
      track.stop();
    } catch (_) {}
    remoteTracks.delete(MCU_AUDIO_UID);
  }
  $("remoteAudio").textContent = "客服已离开";
}

function clearAudioRetry() {
  if (audioRetryTimer) {
    clearTimeout(audioRetryTimer);
    audioRetryTimer = null;
  }
  audioRetryDelayMs = 1000;
  audioRetryErrorReported = false;
}

function scheduleAudioRetry() {
  if (
    !client ||
    ending ||
    audioRetryTimer ||
    remoteTracks.has(MCU_AUDIO_UID)
  ) {
    return;
  }
  const delay = audioRetryDelayMs;
  audioRetryDelayMs = Math.min(
    AUDIO_RETRY_MAX_MS,
    Math.max(1000, audioRetryDelayMs * 2),
  );
  $("remoteAudio").textContent = "等待重试";
  audioRetryTimer = setTimeout(async () => {
    audioRetryTimer = null;
    if (!client || ending || remoteTracks.has(MCU_AUDIO_UID)) {
      return;
    }
    await subscribeRemoteAudio();
  }, delay);
}

async function subscribeRemoteAudio() {
  if (!client || remoteTracks.has(MCU_AUDIO_UID)) {
    return;
  }
  if (audioSubscriptionPromise) {
    return audioSubscriptionPromise;
  }
  const currentClient = client;
  audioSubscriptionPromise = (async () => {
    let lastError;
    let track;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        $("remoteAudio").textContent = "正在订阅";
        track = await currentClient.subscribe(MCU_AUDIO_UID, "audio");
        break;
      } catch (error) {
        lastError = error;
        if (attempt < 3) {
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      }
    }
    if (!track) {
      throw lastError;
    }
    if (client !== currentClient) {
      try {
        track.stop();
      } catch (_) {}
      return;
    }
    remoteTracks.set(MCU_AUDIO_UID, track);
    clearAudioRetry();
    try {
      await track.play();
      $("resumeAudio").hidden = true;
      $("remoteAudio").textContent = "正在播放";
    } catch (_) {
      $("resumeAudio").hidden = false;
      $("remoteAudio").textContent = "等待播放授权";
    }
  })()
    .catch((error) => {
      const code = error?.code || error?.name || "UNKNOWN";
      if (!audioRetryErrorReported) {
        audioRetryErrorReported = true;
        setError(
          `订阅客服音频失败，将自动重试（${code}）：${errorMessage(error, "未知错误")}`,
        );
      }
      scheduleAudioRetry();
    })
    .finally(() => {
      audioSubscriptionPromise = null;
    });
  return audioSubscriptionPromise;
}

function configureRtcClient() {
  client = sdk.createClient();
  client.on("user-published", (user, mediaType) => {
    if (mediaType === "audio") {
      subscribeRemoteAudio();
    }
  });
  client.on("user-left", (user) => {
    stopRemoteAudio();
    if (!client || client.remoteUsers.length === 0) {
      $("remoteAudio").textContent = "客服已离开";
    }
  });
  client.on("user-unpublished", (_user, mediaType) => {
    if (mediaType === "audio") {
      stopRemoteAudio();
    }
  });
}

async function connectEvents(ticket) {
  clearTimeout(reconnectTimer);
  if (ws) {
    ws.onclose = null;
    ws.close();
  }
  const wsBase = config.backendUrl.replace(/^http/, "ws");
  ws = new WebSocket(
    `${wsBase}/ws?ticket=${encodeURIComponent(ticket)}`,
  );
  ws.onmessage = (event) => {
    if (event.data === "pong") {
      return;
    }
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (message.type === "call" && message.call) {
      applyCall(message.call);
    }
  };
  ws.onclose = () => {
    if (!ending && call) {
      setError("状态连接已断开，正在重连。");
      reconnectTimer = setTimeout(reconnectEvents, 1800);
    }
  };
}

async function reconnectEvents() {
  if (!call || !callerToken || ending) {
    return;
  }
  try {
    const response = await fetch(
      `${config.backendUrl}/api/calls/${call.id}/ws-ticket`,
      {
        method: "POST",
        headers: { "X-Caller-Token": callerToken },
      },
    );
    const body = await readJson(response);
    await connectEvents(body.ticket);
    setError("");
  } catch (error) {
    reconnectTimer = setTimeout(reconnectEvents, 2500);
  }
}

function applyCall(nextCall) {
  call = nextCall;
  $("sessionId").textContent = call.id;
  setBadge(call.status);
  if (call.status === "waiting") {
    $("statusText").textContent = "音频已发布，正在等待客服接听。";
  } else if (call.status === "connecting") {
    $("statusText").textContent = "客服已接听，正在建立通话。";
  } else if (call.status === "active") {
    $("statusText").textContent = "双向音频已建立。";
  } else if (call.status === "interrupted") {
    $("statusText").textContent = "连接中断，正在等待恢复。";
  }
  if (call.status === "ended") {
    const reason = END_REASON_LABELS[call.endReason] || call.endReason || "已结束";
    $("statusText").textContent = `会话已结束：${reason}`;
    finishLocal(false);
  }
}

async function startCall() {
  if (call || ending) {
    return;
  }
  setError("");
  setBusy(true);
  setBadge("created");
  $("statusText").textContent = "正在创建会话。";
  try {
    const name = $("displayName").value.trim() || config.defaultName;
    const response = await fetch(`${config.backendUrl}/api/calls`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_id: config.clientId,
        client_name: name,
      }),
    });
    const body = await readJson(response);
    call = body.call;
    callerToken = body.callerToken;
    $("sessionId").textContent = call.id;
    $("statusText").textContent = "会话已创建，正在申请本机麦克风。";
    await checkMicrophonePermission();
    microphone = await withTimeout(
      sdk.createMicrophoneAudioTrack(),
      MICROPHONE_TIMEOUT_MS,
      "申请麦克风超时，请检查系统麦克风权限。",
    );
    if (ending) {
      microphone.close();
      microphone = null;
      return;
    }
    configureRtcClient();
    $("statusText").textContent = "麦克风已获取，正在加入 DingRTC 房间。";
    await withTimeout(
      client.join(body.rtc),
      RTC_JOIN_TIMEOUT_MS,
      "加入 DingRTC 房间超时。",
    );
    if (ending) {
      return;
    }
    if (client.remoteUsers.some((user) => user.hasAudio)) {
      await subscribeRemoteAudio();
    }
    $("statusText").textContent = "已加入房间，正在发布麦克风音频。";
    await withTimeout(
      client.publish([microphone]),
      RTC_PUBLISH_TIMEOUT_MS,
      "发布麦克风音频超时。",
    );
    setBadge("waiting");
    $("statusText").textContent = "麦克风已发布，正在等待客服接听。";

    const readyResponse = await fetch(
      `${config.backendUrl}/api/calls/${call.id}/ready`,
      {
        method: "POST",
        headers: { "X-Caller-Token": callerToken },
      },
    );
    const readyBody = await readJson(readyResponse);
    applyCall(readyBody.call);
    await connectEvents(body.wsTicket);
    heartbeatTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send("ping");
      }
    }, 10000);
  } catch (error) {
    const message = errorMessage(error, "呼叫失败");
    setError(`呼叫失败：${message}`);
    await finishLocal(true);
    $("statusText").textContent = "呼叫失败，请查看错误信息。";
  } finally {
    setBusy(Boolean(call && call.status !== "ended"));
  }
}

async function cleanupRtc() {
  clearAudioRetry();
  await stopScreenShare(false);
  stopRemoteTracks();
  if (microphone) {
    try {
      microphone.close();
    } catch (_) {}
    microphone = null;
  }
  if (client) {
    const current = client;
    client = null;
    try {
      await Promise.race([
        current.leave(),
        new Promise((_, reject) => setTimeout(() => reject(new Error("LEAVE_TIMEOUT")), 5000)),
      ]);
    } catch (_) {}
  }
}

async function finishLocal(sendEnd) {
  if (ending) {
    return;
  }
  ending = true;
  clearInterval(heartbeatTimer);
  clearTimeout(reconnectTimer);
  if (sendEnd && call && callerToken) {
    try {
      await withTimeout(
        fetch(`${config.backendUrl}/api/calls/${call.id}/end`, {
          method: "POST",
          headers: { "X-Caller-Token": callerToken },
        }),
        5000,
        "结束会话请求超时。",
      );
    } catch (_) {}
  }
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  await cleanupRtc();
  call = null;
  callerToken = null;
  muted = false;
  setBadge("idle");
  $("sessionId").textContent = "-";
  $("mute").textContent = "静音";
  setBusy(false);
  setTimeout(() => {
    ending = false;
  }, 0);
}

$("startCall").addEventListener("click", startCall);

$("mute").addEventListener("click", async () => {
  if (!client || !microphone || ending) {
    return;
  }
  $("mute").disabled = true;
  try {
    if (muted) {
      await client.publish([microphone]);
    } else {
      await client.unpublish([microphone]);
    }
    muted = !muted;
    $("mute").textContent = muted ? "取消静音" : "静音";
    $("statusText").textContent = muted ? "本机麦克风已静音。" : "本机麦克风已恢复。";
  } catch (error) {
    setError(`静音切换失败：${error.code || error.name || "UNKNOWN"}`);
  } finally {
    $("mute").disabled = false;
  }
});

$("shareScreen").addEventListener("click", async () => {
  if (screenTrack) {
    await stopScreenShare(true);
  } else {
    await startScreenShare();
  }
});

$("resumeAudio").addEventListener("click", async () => {
  try {
    for (const track of remoteTracks.values()) {
      await track.play();
    }
    $("resumeAudio").hidden = true;
    $("remoteAudio").textContent = "正在播放";
  } catch (_) {
    setError("远端声音仍未能播放，请检查系统扬声器。");
  }
});

$("hangup").addEventListener("click", () => finishLocal(true));

window.addEventListener("pagehide", () => {
  if (call && callerToken && !ending) {
    fetch(`${config.backendUrl}/api/calls/${call.id}/end`, {
      method: "POST",
      headers: { "X-Caller-Token": callerToken },
      keepalive: true,
    }).catch(() => {});
  }
});

async function initialize() {
  config = await fetch("/config.json").then((response) => response.json());
  $("clientId").value = config.clientId;
  $("displayName").value = config.defaultName;
  $("displayName").addEventListener("change", () => {
    $("displayName").value = $("displayName").value.trim().slice(0, 64);
  });
  await checkBackend();
}

initialize().catch((error) => {
  setError(`页面初始化失败：${error.message || error}`);
});
