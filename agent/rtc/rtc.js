"use strict";

const sdk = window.DingRTC.default || window.DingRTC;
const $ = (id) => document.getElementById(id);

let bridge;
let client;
let microphone;
let muted = false;
let starting = false;
let stopRequested = false;
let remoteTracks = new Map();
let audioSubscriptionPromise = null;
let audioRetryTimer = null;
let audioRetryDelayMs = 1000;
let audioRetryErrorReported = false;
let screenSubscriptionPromise = null;
let screenRetryTimer = null;
let screenRetryUserId = null;
let screenRetryDelayMs = 1000;
let screenRetryErrorReported = false;

const MICROPHONE_TIMEOUT_MS = 20000;
const RTC_JOIN_TIMEOUT_MS = 20000;
const RTC_PUBLISH_TIMEOUT_MS = 15000;
const MCU_AUDIO_UID = "mcu";
const SCREEN_TRACK_PREFIX = "screen:";
const AUDIO_RETRY_MAX_MS = 5000;
const SCREEN_RETRY_MAX_MS = 5000;

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function emit(type, data = {}) {
  if (bridge) {
    bridge.emitEvent(JSON.stringify({ type, ...data }));
  }
}

function setState(status, message) {
  const labels = {
    idle: "RTC 未启动",
    connecting: "正在连接 RTC",
    active: "RTC 音频已连接",
    error: "RTC 连接异常",
  };
  $("signal").className = `signal ${status}`;
  $("state").textContent = labels[status] || status;
  $("message").textContent = message || "";
}

function errorMessage(error, fallback) {
  return error?.message || error?.code || error?.name || fallback;
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
    stopRequested ||
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
  audioRetryTimer = setTimeout(async () => {
    audioRetryTimer = null;
    if (!client || stopRequested || remoteTracks.has(MCU_AUDIO_UID)) {
      return;
    }
    await subscribeRemoteAudio();
  }, delay);
}

function clearScreenRetry() {
  if (screenRetryTimer) {
    clearTimeout(screenRetryTimer);
    screenRetryTimer = null;
  }
  screenRetryUserId = null;
  screenRetryDelayMs = 1000;
  screenRetryErrorReported = false;
}

function scheduleScreenRetry(userId) {
  if (
    !client ||
    !userId ||
    stopRequested ||
    screenRetryTimer ||
    remoteTracks.has(`${SCREEN_TRACK_PREFIX}${userId}`)
  ) {
    return;
  }
  screenRetryUserId = userId;
  const delay = screenRetryDelayMs;
  screenRetryDelayMs = Math.min(
    SCREEN_RETRY_MAX_MS,
    Math.max(1000, screenRetryDelayMs * 2),
  );
  $("screenState").textContent = "等待重试";
  screenRetryTimer = setTimeout(async () => {
    screenRetryTimer = null;
    if (!client || stopRequested || screenRetryUserId !== userId) {
      return;
    }
    const user = client.remoteUsers.find(
      (item) => item.userId === userId && item.hasAuxiliary,
    );
    if (!user) {
      $("screenState").textContent = "未共享";
      screenRetryUserId = null;
      return;
    }
    await subscribeRemoteScreen(user);
  }, delay);
}

function stopRemoteScreen(userId) {
  if (!userId || screenRetryUserId === userId) {
    clearScreenRetry();
  }
  const prefix = userId
    ? `${SCREEN_TRACK_PREFIX}${userId}`
    : SCREEN_TRACK_PREFIX;
  for (const [key, track] of remoteTracks.entries()) {
    if (!key.startsWith(prefix)) {
      continue;
    }
    try {
      track.stop();
    } catch (_) {}
    remoteTracks.delete(key);
  }
  $("remoteScreen").replaceChildren();
  $("screenState").textContent = "未共享";
  emit("remote_screen", { playing: false, userId: userId || null });
}

async function subscribeRemoteScreen(user) {
  if (!client || !user?.userId) {
    return;
  }
  const key = `${SCREEN_TRACK_PREFIX}${user.userId}`;
  if (remoteTracks.has(key)) {
    return;
  }
  if (screenSubscriptionPromise) {
    return screenSubscriptionPromise;
  }
  screenRetryUserId = user.userId;
  const currentClient = client;
  screenSubscriptionPromise = (async () => {
    let lastError;
    let track;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        $("screenState").textContent = "正在订阅";
        track = await currentClient.subscribe(user.userId, "video", true);
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
    if (screenRetryUserId !== user.userId) {
      try {
        track.stop();
      } catch (_) {}
      return;
    }
    remoteTracks.set(key, track);
    clearScreenRetry();
    track.on("track-ended", () => stopRemoteScreen(user.userId));
    $("screenState").textContent = "正在播放";
    track.play("remoteScreen", { fit: "contain" });
    emit("remote_screen", { playing: true, userId: user.userId });
  })()
    .catch((error) => {
      if (!screenRetryErrorReported) {
        screenRetryErrorReported = true;
        emit("error", {
          code: error?.code || error?.name || "SCREEN_SUBSCRIBE_FAILED",
          message: `订阅呼叫端屏幕失败，将自动重试：${errorMessage(error, "未知错误")}`,
        });
      }
      scheduleScreenRetry(user.userId);
    })
    .finally(() => {
      screenSubscriptionPromise = null;
    });
  return screenSubscriptionPromise;
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
      $("play").hidden = true;
      emit("remote_audio", { playing: true });
    } catch (_) {
      $("play").hidden = false;
      emit("remote_audio", { playing: false, needsGesture: true });
    }
  })()
    .catch((error) => {
      if (!audioRetryErrorReported) {
        audioRetryErrorReported = true;
        emit("error", {
          code: error.code || error.name || "SUBSCRIBE_FAILED",
          message: `订阅呼叫端音频失败，将自动重试：${errorMessage(error, "未知错误")}`,
        });
      }
      scheduleAudioRetry();
    })
    .finally(() => {
      audioSubscriptionPromise = null;
    });
  return audioSubscriptionPromise;
}

function buildClient() {
  client = sdk.createClient();
  client.on("user-published", (user, mediaType, auxiliary) => {
    if (mediaType === "audio") {
      subscribeRemoteAudio();
    } else if (mediaType === "video" && auxiliary) {
      subscribeRemoteScreen(user);
    }
  });
  client.on("user-left", (user) => {
    stopRemoteAudio();
    stopRemoteScreen(user.userId);
    setState("connecting", "呼叫端已离开，正在更新 RTC 状态。");
  });
  client.on("user-unpublished", (user, mediaType, auxiliary) => {
    if (mediaType === "audio") {
      stopRemoteAudio();
    } else if (mediaType === "video" && auxiliary) {
      stopRemoteScreen(user.userId);
    }
  });
}

async function cleanup() {
  for (const track of remoteTracks.values()) {
    try {
      track.stop();
    } catch (_) {}
  }
  remoteTracks.clear();
  clearAudioRetry();
  clearScreenRetry();
  muted = false;
  $("play").hidden = true;
  $("remoteScreen").replaceChildren();
  $("screenState").textContent = "未共享";
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
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("LEAVE_TIMEOUT")), 5000),
        ),
      ]);
    } catch (_) {}
  }
}

window.rtcSession = {
  async start(auth) {
    if (starting || client) {
      return;
    }
    starting = true;
    stopRequested = false;
    setState("connecting", "正在申请客服麦克风。");
    emit("connecting", { roomId: auth.channel });
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("当前 RTC 环境不支持麦克风采集。");
      }
      microphone = await withTimeout(
        sdk.createMicrophoneAudioTrack(),
        MICROPHONE_TIMEOUT_MS,
        "申请客服麦克风超时，请检查 Windows 麦克风权限。",
      );
      if (stopRequested) {
        return;
      }
      emit("media_ready");
      setState("connecting", "麦克风已获取，正在加入 DingRTC 房间。");
      buildClient();
      await withTimeout(
        client.join(auth),
        RTC_JOIN_TIMEOUT_MS,
        "加入 DingRTC 房间超时。",
      );
      if (stopRequested) {
        return;
      }
      emit("joined_room", { roomId: auth.channel });
      setState("connecting", "已加入房间，正在发布客服麦克风。");
      await withTimeout(
        client.publish([microphone]),
        RTC_PUBLISH_TIMEOUT_MS,
        "发布客服麦克风音频超时。",
      );
      if (stopRequested) {
        return;
      }
      if (client.remoteUsers.some((user) => user.hasAudio)) {
        await subscribeRemoteAudio();
      }
      const screenUser = client.remoteUsers.find((user) => user.hasAuxiliary);
      if (screenUser) {
        await subscribeRemoteScreen(screenUser);
      }
      setState("active", "客服麦克风已发布，正在接收呼叫端声音和屏幕。");
      emit("joined", { roomId: auth.channel, uid: auth.uid });
    } catch (error) {
      if (!stopRequested) {
        setState("error", "RTC 建立失败，请检查麦克风和网络。");
        emit("error", {
          code: error.code || error.name || "JOIN_FAILED",
          message: error.message || "RTC 建立失败",
          fatal: true,
        });
        await cleanup();
      }
    } finally {
      starting = false;
      if (stopRequested) {
        await cleanup();
      }
    }
  },

  async stop() {
    stopRequested = true;
    await cleanup();
    setState("idle", "RTC 已离开房间。");
    emit("stopped");
  },

  async setMuted(value) {
    if (!client || !microphone) {
      return;
    }
    try {
      if (value && !muted) {
        await client.unpublish([microphone]);
      } else if (!value && muted) {
        await client.publish([microphone]);
      }
      muted = Boolean(value);
      emit("muted", { muted });
    } catch (error) {
      emit("error", {
        code: error.code || error.name || "MUTE_FAILED",
        message: "静音切换失败",
      });
    }
  },

  async playRemoteAudio() {
    try {
      const audioTrack = remoteTracks.get(MCU_AUDIO_UID);
      if (audioTrack) {
        const track = audioTrack;
        await track.play();
      }
      $("play").hidden = true;
      emit("remote_audio", { playing: true });
    } catch (error) {
      emit("error", {
        code: error.code || error.name || "PLAY_FAILED",
        message: "远端声音播放失败",
      });
    }
  },
};

new QWebChannel(qt.webChannelTransport, (channel) => {
  bridge = channel.objects.qtBridge;
  emit("ready");
});

$("play").addEventListener("click", () => window.rtcSession.playRemoteAudio());
