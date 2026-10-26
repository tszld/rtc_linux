import { AgentRtcSession } from "./rtc.js";
import { NativeAudioSession } from "./native-audio.js";


const ROBOT_STATUS_LABELS = {
  online: "在线",
  degraded: "连接波动",
  offline: "离线",
};

const LEASE_SECONDS = 60;
const RECONNECT_DELAY_MS = 2000;
const PING_INTERVAL_MS = 15000;
const CATALOG_REFRESH_MS = 4000;
const NATIVE_HEARTBEAT_MS = 5000;
const VOLUME_UPDATE_DELAY_MS = 180;
const COMBO_VOICE_UPDATE_DELAY_MS = 240;


class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}


const elements = {
  loginView: document.querySelector("#loginView"),
  appView: document.querySelector("#appView"),
  loginForm: document.querySelector("#loginForm"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  loginButton: document.querySelector("#loginButton"),
  loginError: document.querySelector("#loginError"),
  agentName: document.querySelector("#agentName"),
  controlState: document.querySelector("#controlState"),
  logoutButton: document.querySelector("#logoutButton"),
  refreshSessionsButton: document.querySelector("#refreshSessionsButton"),
  sessionList: document.querySelector("#sessionList"),
  sessionEmpty: document.querySelector("#sessionEmpty"),
  noSelection: document.querySelector("#noSelection"),
  sessionView: document.querySelector("#sessionView"),
  sessionScenario: document.querySelector("#sessionScenario"),
  sessionRobotName: document.querySelector("#sessionRobotName"),
  sessionMeta: document.querySelector("#sessionMeta"),
  sessionStatus: document.querySelector("#sessionStatus"),
  videoFrame: document.querySelector("#videoFrame"),
  remoteVideo: document.querySelector("#remoteVideo"),
  videoEmpty: document.querySelector("#videoEmpty"),
  videoEmptyText: document.querySelector("#videoEmptyText"),
  rtcState: document.querySelector("#rtcState"),
  audioState: document.querySelector("#audioState"),
  audioResumeButton: document.querySelector("#audioResumeButton"),
  robotMicState: document.querySelector("#robotMicState"),
  robotCameraState: document.querySelector("#robotCameraState"),
  listenState: document.querySelector("#listenState"),
  microphoneState: document.querySelector("#microphoneState"),
  voiceState: document.querySelector("#voiceState"),
  leaseTitle: document.querySelector("#leaseTitle"),
  leaseDescription: document.querySelector("#leaseDescription"),
  microphoneTitle: document.querySelector("#microphoneTitle"),
  microphoneDescription: document.querySelector("#microphoneDescription"),
  microphoneIndicator: document.querySelector("#microphoneIndicator"),
  microphoneButton: document.querySelector("#microphoneButton"),
  voiceMode: document.querySelector("#voiceMode"),
  comboVoicePanel: document.querySelector("#comboVoicePanel"),
  comboBaseVoice: document.querySelector("#comboBaseVoice"),
  comboPitch: document.querySelector("#comboPitch"),
  comboPitchValue: document.querySelector("#comboPitchValue"),
  comboReverb: document.querySelector("#comboReverb"),
  comboBeautify: document.querySelector("#comboBeautify"),
  comboLowGain: document.querySelector("#comboLowGain"),
  comboLowGainValue: document.querySelector("#comboLowGainValue"),
  comboMidGain: document.querySelector("#comboMidGain"),
  comboMidGainValue: document.querySelector("#comboMidGainValue"),
  comboHighGain: document.querySelector("#comboHighGain"),
  comboHighGainValue: document.querySelector("#comboHighGainValue"),
  robotAudioVolume: document.querySelector("#robotAudioVolume"),
  robotAudioVolumeValue: document.querySelector("#robotAudioVolumeValue"),
  agentPlaybackVolume: document.querySelector("#agentPlaybackVolume"),
  agentPlaybackVolumeValue: document.querySelector("#agentPlaybackVolumeValue"),
  agentPlaybackVolumeState: document.querySelector("#agentPlaybackVolumeState"),
  takeoverButton: document.querySelector("#takeoverButton"),
  releaseButton: document.querySelector("#releaseButton"),
  joinButton: document.querySelector("#joinButton"),
  leaveButton: document.querySelector("#leaveButton"),
  endSessionButton: document.querySelector("#endSessionButton"),
  terminateRobotButton: document.querySelector("#terminateRobotButton"),
  listenButton: document.querySelector("#listenButton"),
  toastRegion: document.querySelector("#toastRegion"),
};


const state = {
  backendUrl: "",
  configReady: false,
  nativeAudioAvailable: false,
  agentControlToken: "",
  accessToken: "",
  principal: null,
  sessions: [],
  robots: new Map(),
  selectedSessionId: null,
  joinedSessionId: null,
  listening: false,
  lease: null,
  leaseUncertain: false,
  microphoneEnabled: false,
  rtc: null,
  nativeAudio: null,
  talkUserId: null,
  ignoredMediaUserIds: new Set(),
  talkAuth: null,
  voiceMode: 0,
  voiceModeLabels: { 0: "关闭音色改变" },
  comboVoiceDefaults: {
    baseVoiceMode: 3,
    pitch: 1.1,
    reverbMode: 1,
    beautifyMode: 2,
    lowGain: -1,
    midGain: 2,
    highGain: 3,
  },
  comboVoice: null,
  comboVoiceLabels: { reverb: {}, beautify: {} },
  robotAudioVolume: 100,
  agentPlaybackVolume: 100,
  robotMediaUserId: null,
  playbackVolumeTimer: null,
  comboVoiceTimer: null,
  nativeHeartbeatTimer: null,
  talkTokenRefreshTimer: null,
  socket: null,
  socketReconnectTimer: null,
  socketPingTimer: null,
  catalogTimer: null,
  clockTimer: null,
  leaseRenewTimer: null,
  catalogRequest: null,
  tokenRefresh: null,
  loggingOut: false,
  leaving: false,
  leavingSessionId: null,
  endingSessionId: null,
  terminatingRobot: false,
};


function sessionById(sessionId) {
  return state.sessions.find((session) => session.id === sessionId) || null;
}


function selectedSession() {
  return sessionById(state.selectedSessionId);
}


function robotForSession(session) {
  return session ? state.robots.get(session.robotId) || null : null;
}


function labelFor(value, labels) {
  return labels[value] || value || "未知";
}


function sessionDisplayState(session) {
  if (!session) {
    return {
      key: "waiting",
      label: "等待客服接入",
      detail: "请选择业务会话",
      tone: "neutral",
    };
  }
  if (state.endingSessionId === session.id) {
    return {
      key: "ending",
      label: "正在结束",
      detail: "机器人即将退出频道",
      tone: "warning",
    };
  }
  if (state.leavingSessionId === session.id) {
    return {
      key: "leaving",
      label: "正在离开",
      detail: "正在释放当前会话",
      tone: "neutral",
    };
  }
  if (session.status === "ended") {
    return {
      key: "ended",
      label: "会话已结束",
      detail: "本次业务已完成",
      tone: "neutral",
    };
  }
  const robotStatus = session.robotStatus || robotForSession(session)?.status;
  if (robotStatus === "offline") {
    return {
      key: "offline",
      label: "机器人已离线",
      detail: "等待机器人重新上线",
      tone: "danger",
    };
  }
  if (session.takeover) {
    const microphoneEnabled = Boolean(
      session.takeover.microphoneEnabled,
    );
    return {
      key: microphoneEnabled ? "talking" : "takenover",
      label: microphoneEnabled ? "客服通话中" : "客服已接管",
      detail: microphoneEnabled
        ? `麦克风已开 · ${session.takeover.agentDisplayName || "客服"}`
        : "麦克风关闭",
      tone: microphoneEnabled ? "warning" : "success",
      micState: microphoneEnabled ? "on" : "off",
    };
  }
  if (session.claim) {
    const isMine = session.claim.agentId === state.principal?.id;
    return {
      key: isMine ? "mine" : "occupied",
      label: isMine ? "你正在处理" : "其他客服处理中",
      detail: isMine
        ? "可以继续听取或开始接管"
        : `已被${session.claim.agentDisplayName || "其他客服"}接入`,
      tone: isMine ? "success" : "neutral",
    };
  }
  return {
    key: "waiting",
    label: "等待客服接入",
    detail: "选择后即可接入会话",
    tone: "warning",
  };
}


function formatTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}


function formatDuration(startValue) {
  const started = new Date(startValue).getTime();
  if (!Number.isFinite(started)) {
    return "00:00";
  }
  const total = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}


function leaseRemainingSeconds() {
  if (!state.lease?.expiresAt) {
    return 0;
  }
  const expires = new Date(state.lease.expiresAt).getTime();
  if (!Number.isFinite(expires)) {
    return 0;
  }
  return Math.max(0, Math.ceil((expires - Date.now()) / 1000));
}


function errorText(error) {
  if (error instanceof ApiError || error instanceof Error) {
    return error.message;
  }
  return String(error || "操作失败");
}


function voiceModeLabel(mode) {
  return state.voiceModeLabels[Number(mode)] || `模式 ${mode}`;
}


function updateComboVoiceOutputs() {
  const voice = state.comboVoice || state.comboVoiceDefaults;
  elements.comboPitchValue.textContent = Number(voice.pitch).toFixed(2);
  elements.comboLowGainValue.textContent = `${Number(voice.lowGain).toFixed(0)} dB`;
  elements.comboMidGainValue.textContent = `${Number(voice.midGain).toFixed(0)} dB`;
  elements.comboHighGainValue.textContent = `${Number(voice.highGain).toFixed(0)} dB`;
}


function setComboVoiceControls(voice) {
  state.comboVoice = { ...voice };
  elements.comboBaseVoice.value = String(voice.baseVoiceMode);
  elements.comboPitch.value = String(voice.pitch);
  elements.comboReverb.value = String(voice.reverbMode);
  elements.comboBeautify.value = String(voice.beautifyMode);
  elements.comboLowGain.value = String(voice.lowGain);
  elements.comboMidGain.value = String(voice.midGain);
  elements.comboHighGain.value = String(voice.highGain);
  updateComboVoiceOutputs();
}


function readComboVoiceControls() {
  return {
    baseVoiceMode: Number(elements.comboBaseVoice.value),
    pitch: Number(elements.comboPitch.value),
    reverbMode: Number(elements.comboReverb.value),
    beautifyMode: Number(elements.comboBeautify.value),
    lowGain: Number(elements.comboLowGain.value),
    midGain: Number(elements.comboMidGain.value),
    highGain: Number(elements.comboHighGain.value),
  };
}


function scheduleComboVoiceApply() {
  window.clearTimeout(state.comboVoiceTimer);
  state.comboVoiceTimer = window.setTimeout(() => {
    state.comboVoiceTimer = null;
    if (!state.nativeAudio?.sessionId || state.voiceMode !== 100) {
      return;
    }
    void state.nativeAudio
      .setVoiceMode(100, state.comboVoice)
      .catch((error) => {
        showToast(`组合音色应用失败：${errorText(error)}`, "danger");
      });
  }, COMBO_VOICE_UPDATE_DELAY_MS);
}


function handleComboVoiceInput() {
  state.comboVoice = readComboVoiceControls();
  state.voiceMode = 100;
  elements.voiceMode.value = "100";
  updateComboVoiceOutputs();
  render();
  scheduleComboVoiceApply();
}


function applyRobotAudioVolume() {
  if (!state.robotMediaUserId) {
    return;
  }
  state.rtc?.setRemoteAudioVolume(
    state.robotMediaUserId,
    state.robotAudioVolume,
  );
}


function updateRobotAudioVolume(value) {
  state.robotAudioVolume = Math.max(0, Math.min(100, Number(value)));
  elements.robotAudioVolumeValue.textContent = `${state.robotAudioVolume}%`;
  elements.robotAudioVolume.value = String(state.robotAudioVolume);
  applyRobotAudioVolume();
}


function scheduleAgentPlaybackVolumeUpdate() {
  window.clearTimeout(state.playbackVolumeTimer);
  state.playbackVolumeTimer = window.setTimeout(() => {
    state.playbackVolumeTimer = null;
    void sendAgentPlaybackVolume();
  }, VOLUME_UPDATE_DELAY_MS);
}


async function sendAgentPlaybackVolume() {
  const sessionId = state.joinedSessionId;
  if (!sessionId) {
    return;
  }
  elements.agentPlaybackVolumeState.textContent = "正在下发机器人播放音量...";
  try {
    const result = await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(sessionId)}/agent-playback-volume`,
      {
        method: "POST",
        body: { volume: state.agentPlaybackVolume },
      },
    );
    elements.agentPlaybackVolumeState.textContent = result.delivered
      ? `机器人播放音量已设置为 ${state.agentPlaybackVolume}%。`
      : "机器人控制连接未送达，等待机器人重新连接后再调整。";
  } catch (error) {
    elements.agentPlaybackVolumeState.textContent = `机器人播放音量设置失败：${errorText(error)}`;
  }
}


function updateAgentPlaybackVolume(value) {
  state.agentPlaybackVolume = Math.max(0, Math.min(100, Number(value)));
  elements.agentPlaybackVolumeValue.textContent = `${state.agentPlaybackVolume}%`;
  elements.agentPlaybackVolume.value = String(state.agentPlaybackVolume);
  scheduleAgentPlaybackVolumeUpdate();
}


function requireNativeAudio() {
  if (!state.nativeAudioAvailable || !state.agentControlToken) {
    throw new Error("Windows ARTC 音频助手未就绪");
  }
  if (!state.nativeAudio) {
    state.nativeAudio = new NativeAudioSession({
      controlToken: state.agentControlToken,
      onEvent: handleNativeAudioEvent,
    });
  }
  return state.nativeAudio;
}


function startNativeHeartbeat() {
  window.clearInterval(state.nativeHeartbeatTimer);
  state.nativeHeartbeatTimer = window.setInterval(() => {
    if (!state.nativeAudio?.sessionId) {
      return;
    }
    void state.nativeAudio.heartbeat().catch((error) => {
      void stopNativeAudio();
      showToast(`Windows 音频连接已停止：${errorText(error)}`, "danger");
    });
  }, NATIVE_HEARTBEAT_MS);
}


function stopNativeHeartbeat() {
  window.clearInterval(state.nativeHeartbeatTimer);
  state.nativeHeartbeatTimer = null;
}


function scheduleTalkTokenRefresh(auth) {
  window.clearTimeout(state.talkTokenRefreshTimer);
  const expiresAt = Number(auth?.expiresAt || 0) * 1000;
  const delay = Number.isFinite(expiresAt) && expiresAt > 0
    ? Math.max(5000, expiresAt - Date.now() - 60000)
    : 30000;
  state.talkTokenRefreshTimer = window.setTimeout(() => {
    void refreshTalkToken();
  }, delay);
}


async function refreshTalkToken() {
  if (!state.lease?.sessionId || !state.nativeAudio?.sessionId) {
    return;
  }
  const sessionId = state.lease.sessionId;
  try {
    const auth = await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(sessionId)}/talk-token`,
      { method: "POST", body: {} },
    );
    if (state.lease?.sessionId !== sessionId || !state.nativeAudio?.sessionId) {
      return;
    }
    await state.nativeAudio.refreshToken(auth.token);
    state.talkAuth = auth;
    scheduleTalkTokenRefresh(auth);
  } catch (error) {
    await stopNativeAudio();
    showToast(`麦克风凭证刷新失败：${errorText(error)}`, "danger");
  }
}


async function startNativeAudioForLease(sessionId) {
  const auth = await apiRequest(
    `/api/robot-sessions/${encodeURIComponent(sessionId)}/talk-token`,
    { method: "POST", body: {} },
  );
  const nativeAudio = requireNativeAudio();
  const status = await nativeAudio.start(
    auth,
    state.voiceMode,
    state.voiceMode === 100 ? state.comboVoice : null,
  );
  state.talkAuth = auth;
  state.talkUserId = status.userId || auth.userId;
  state.ignoredMediaUserIds.add(state.talkUserId);
  state.rtc?.muteRemoteAudio(state.talkUserId, true);
  scheduleTalkTokenRefresh(auth);
  startNativeHeartbeat();
}


async function stopNativeAudio({ keepalive = false } = {}) {
  stopNativeHeartbeat();
  window.clearTimeout(state.talkTokenRefreshTimer);
  window.clearTimeout(state.playbackVolumeTimer);
  window.clearTimeout(state.comboVoiceTimer);
  state.talkTokenRefreshTimer = null;
  const nativeAudio = state.nativeAudio;
  if (!nativeAudio?.sessionId) {
    state.microphoneEnabled = false;
    state.talkAuth = null;
    state.talkUserId = null;
    return;
  }
  const sessionId = state.lease?.sessionId;
  const shouldReportMicrophone = Boolean(
    state.microphoneEnabled || nativeAudio.microphoneEnabled,
  );
  try {
    if (nativeAudio.microphoneEnabled) {
      await nativeAudio.setMicrophone(false);
    }
  } catch (_error) {
    // 助手异常时仍继续请求进程退出。
  }
  try {
    await nativeAudio.stop({ keepalive });
  } catch (_error) {
    // 页面退出或助手已经退出时不再阻塞后续控制面清理。
  }
  if (shouldReportMicrophone && sessionId) {
    await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(sessionId)}/takeover-lease/microphone`,
      { method: "POST", body: { enabled: false } },
    ).catch(() => {});
  }
  state.microphoneEnabled = false;
  state.talkAuth = null;
  state.talkUserId = null;
}


function handleNativeAudioEvent(event) {
  switch (event.type) {
    case "connected":
      elements.voiceState.textContent = voiceModeLabel(event.voiceMode);
      break;
    case "microphone":
      state.microphoneEnabled = Boolean(event.enabled);
      render();
      break;
    case "voice":
      state.voiceMode = Number(event.mode);
      elements.voiceState.textContent = voiceModeLabel(state.voiceMode);
      break;
    case "token-refreshed":
      break;
    default:
      break;
  }
}


function showToast(message, tone = "neutral") {
  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;
  toast.textContent = message;
  elements.toastRegion.append(toast);
  window.setTimeout(() => {
    toast.classList.add("leaving");
    window.setTimeout(() => toast.remove(), 220);
  }, 3200);
}


function setLoginError(message = "") {
  elements.loginError.textContent = message;
  elements.loginError.hidden = !message;
}


function setLoginBusy(busy) {
  elements.loginButton.disabled = busy || !state.configReady;
  elements.loginButton.textContent = busy ? "正在登录" : "登录";
  elements.username.disabled = busy;
  elements.password.disabled = busy;
}


function setControlState(label, status = "neutral") {
  elements.controlState.dataset.state = status;
  elements.controlState.className = `status-pill ${status}`;
  elements.controlState.querySelector(".status-text").textContent = label;
}


async function apiRequest(
  path,
  {
    method = "GET",
    body = null,
    keepalive = false,
    authenticated = true,
  } = {},
) {
  if (!state.backendUrl) {
    throw new ApiError("客服工作台尚未加载业务控制端地址");
  }
  const headers = { Accept: "application/json" };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
  }
  if (authenticated && state.accessToken) {
    headers.Authorization = `Bearer ${state.accessToken}`;
  }

  let response;
  try {
    response = await fetch(`${state.backendUrl}${path}`, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
      keepalive,
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiError(`无法连接业务后端：${errorText(error)}`);
  }

  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
  }

  if (!response.ok) {
    const message = payload?.detail || `HTTP ${response.status}`;
    throw new ApiError(String(message), response.status);
  }
  return payload;
}


function setAuthenticatedSession(login) {
  state.accessToken = String(login.accessToken || "");
  state.principal = login.principal || null;
  elements.agentName.textContent = state.principal?.displayName || "";
}


function showWorkspace() {
  elements.loginView.hidden = true;
  elements.appView.hidden = false;
}


function showLogin() {
  elements.appView.hidden = true;
  elements.loginView.hidden = false;
  elements.password.value = "";
  setLoginError("");
  setLoginBusy(false);
  elements.username.focus();
}


async function handleLogin(event) {
  event.preventDefault();
  if (!state.configReady) {
    setLoginError("客服工作台配置尚未加载");
    return;
  }
  setLoginError("");
  const username = elements.username.value.trim();
  const password = elements.password.value;
  if (!username || !password) {
    setLoginError("请输入客服账号和密码");
    return;
  }

  setLoginBusy(true);
  try {
    const login = await apiRequest("/api/auth/login", {
      method: "POST",
      body: { username, password },
      authenticated: false,
    });
    setAuthenticatedSession(login);
    if (state.principal?.role === "admin") {
      await apiRequest("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
      state.accessToken = "";
      window.location.assign("/admin.html");
      return;
    }
    elements.password.value = "";
    showWorkspace();
    await refreshCatalog();
    connectControlSocket();
    startPolling();
  } catch (error) {
    setLoginError(errorText(error));
    state.accessToken = "";
    state.principal = null;
  } finally {
    setLoginBusy(false);
  }
}


async function refreshCatalog({ silent = false } = {}) {
  if (state.catalogRequest) {
    return state.catalogRequest;
  }
  state.catalogRequest = (async () => {
    try {
      const [sessions, robots] = await Promise.all([
        apiRequest("/api/robot-sessions"),
        apiRequest("/api/robots"),
      ]);
      applySessions(Array.isArray(sessions) ? sessions : []);
      state.robots = new Map(
        (Array.isArray(robots) ? robots : []).map((robot) => [robot.id, robot]),
      );
      if (
        state.selectedSessionId
        && !state.sessions.some((session) => session.id === state.selectedSessionId)
      ) {
        state.selectedSessionId = null;
      }
      if (
        state.joinedSessionId
        && !state.sessions.some((session) => session.id === state.joinedSessionId)
      ) {
        await leaveSession({
          silent: true,
          releaseTakeover: false,
          reason: "业务会话已结束",
        });
      }
      reconcileUncertainLease();
      render();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        await forceLogout("登录已失效，请重新登录");
        return;
      }
      if (!silent) {
        showToast(errorText(error), "danger");
      }
    } finally {
      state.catalogRequest = null;
    }
  })();
  return state.catalogRequest;
}


function reconcileUncertainLease() {
  if (!state.leaseUncertain || !state.lease?.sessionId) {
    return;
  }
  const session = sessionById(state.lease.sessionId);
  const takeover = session?.takeover;
  if (
    takeover
    && takeover.agentId === state.principal?.id
    && takeover.leaseId === state.lease.id
  ) {
    state.lease = {
      ...state.lease,
      status: "active",
      expiresAt: takeover.expiresAt,
    };
    state.leaseUncertain = false;
    scheduleLeaseRenewal();
    return;
  }
  state.lease = null;
  state.leaseUncertain = false;
  window.clearTimeout(state.leaseRenewTimer);
}


function applySessions(sessions) {
  state.sessions = [...sessions].sort((left, right) => {
    return String(right.startedAt || "").localeCompare(String(left.startedAt || ""));
  });
}


function upsertSession(session) {
  if (!session?.id) {
    return;
  }
  const existingIndex = state.sessions.findIndex((item) => item.id === session.id);
  if (existingIndex >= 0) {
    state.sessions[existingIndex] = {
      ...state.sessions[existingIndex],
      ...session,
    };
  } else {
    state.sessions.unshift(session);
  }
  state.sessions.sort((left, right) => {
    return String(right.startedAt || "").localeCompare(String(left.startedAt || ""));
  });
}


function updateTakeover(sessionId, lease) {
  const session = sessionById(sessionId);
  if (!session) {
    return;
  }
  if (lease?.status === "active") {
    session.status = "agent_takeover";
    session.takeover = {
      leaseId: lease.id,
      agentId: lease.agentId,
      agentDisplayName: lease.agentDisplayName || "客服",
      expiresAt: lease.expiresAt,
      microphoneEnabled: Boolean(lease.microphoneEnabled),
    };
  } else {
    session.status = "active";
    session.takeover = null;
  }
}


function render() {
  renderSessionList();
  renderSessionWorkspace();
}


function renderSessionList() {
  elements.sessionList.replaceChildren();
  elements.sessionEmpty.hidden = state.sessions.length > 0;

  for (const session of state.sessions) {
    const robot = robotForSession(session);
    const displayState = sessionDisplayState(session);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item";
    button.dataset.selected = String(session.id === state.selectedSessionId);
    button.setAttribute("role", "listitem");

    const top = document.createElement("span");
    top.className = "session-item-top";
    const robotName = document.createElement("strong");
    robotName.textContent = session.robotDisplayName || robot?.displayName || "机器人";
    const status = document.createElement("span");
    status.className = `session-tag ${displayState.key}`;
    status.textContent = displayState.label;
    top.append(robotName, status);

    const scenario = document.createElement("span");
    scenario.className = "session-scenario";
    scenario.textContent = session.scenarioName || robot?.scenarioName || "业务场景";

    const footer = document.createElement("span");
    footer.className = "session-item-footer";
    const duration = document.createElement("span");
    duration.textContent = formatDuration(session.startedAt);
    const occupancy = document.createElement("span");
    occupancy.textContent = displayState.detail;
    if (displayState.micState) {
      occupancy.classList.add(`mic-${displayState.micState}`);
    }
    footer.append(duration, occupancy);

    button.append(top, scenario, footer);
    button.addEventListener("click", () => selectSession(session.id));
    elements.sessionList.append(button);
  }
}


function renderSessionWorkspace() {
  const session = selectedSession();
  elements.noSelection.hidden = Boolean(session);
  elements.sessionView.hidden = !session;
  if (!session) {
    return;
  }

  const robot = robotForSession(session);
  const media = robot?.media || {};
  const displayState = sessionDisplayState(session);
  const isJoined = state.joinedSessionId === session.id;
  const ownLease = state.lease?.sessionId === session.id;
  const takeoverOwnedByMe = session.takeover?.agentId === state.principal?.id;
  const foreignLease = Boolean(session.takeover && !takeoverOwnedByMe);
  const unmanagedOwnLease = takeoverOwnedByMe && !ownLease;
  const claimOwnedByMe = session.claim?.agentId === state.principal?.id;
  const claimOwnedByOther = Boolean(
    session.claim && !claimOwnedByMe,
  );

  elements.sessionScenario.textContent = session.scenarioName || robot?.scenarioName || "业务场景";
  elements.sessionRobotName.textContent = session.robotDisplayName || robot?.displayName || "机器人";
  elements.sessionMeta.textContent = [
    `会话 ${String(session.id).slice(0, 8)}`,
    `开始 ${formatTime(session.startedAt)}`,
    `机器人 ${labelFor(session.robotStatus || robot?.status, ROBOT_STATUS_LABELS)}`,
  ].join(" · ");
  elements.sessionStatus.className = `status-pill ${displayState.tone}`;
  elements.sessionStatus.textContent = displayState.label;

  elements.robotMicState.textContent = media.microphonePublished ? "发布中" : "关闭";
  elements.robotCameraState.textContent = media.cameraPublished ? "发布中" : "关闭";
  elements.listenState.textContent = isJoined && state.listening ? "听取中" : "未接入";
  elements.microphoneState.textContent = state.microphoneEnabled ? "发布中" : "关闭";
  elements.microphoneState.classList.toggle("active", state.microphoneEnabled);
  elements.voiceState.textContent = voiceModeLabel(state.voiceMode);
  elements.voiceMode.value = String(state.voiceMode);

  elements.joinButton.hidden = false;
  elements.joinButton.disabled = isJoined || claimOwnedByOther;
  elements.joinButton.textContent = isJoined
    ? "已接入"
    : claimOwnedByOther
      ? `${session.claim.agentDisplayName || "其他客服"}正在处理`
      : "开始处理";
  elements.leaveButton.disabled = !isJoined;
  elements.endSessionButton.disabled = !isJoined;
  elements.terminateRobotButton.disabled = (
    !isJoined || state.terminatingRobot
  );
  elements.listenButton.disabled = !isJoined;
  elements.listenButton.textContent = state.listening ? "停止听取" : "开始听取";

  elements.takeoverButton.disabled = (
    !isJoined
    || !claimOwnedByMe
    || Boolean(session.takeover)
    || Boolean(state.lease)
    || !state.nativeAudioAvailable
  );
  elements.releaseButton.disabled = !ownLease || state.leaseUncertain;
  elements.microphoneButton.disabled = (
    !ownLease
    || state.leaseUncertain
    || leaseRemainingSeconds() <= 0
    || !state.nativeAudioAvailable
  );
  elements.voiceMode.disabled = (
    !isJoined
    || !state.nativeAudioAvailable
  );
  const comboControlsEnabled = isJoined && state.nativeAudioAvailable;
  elements.comboVoicePanel.hidden = state.voiceMode !== 100;
  for (const control of [
    elements.comboBaseVoice,
    elements.comboPitch,
    elements.comboReverb,
    elements.comboBeautify,
    elements.comboLowGain,
    elements.comboMidGain,
    elements.comboHighGain,
  ]) {
    control.disabled = !comboControlsEnabled;
  }
  elements.robotAudioVolume.disabled = !isJoined;
  elements.agentPlaybackVolume.disabled = !isJoined;
  elements.robotAudioVolumeValue.textContent = `${state.robotAudioVolume}%`;
  elements.agentPlaybackVolumeValue.textContent = `${state.agentPlaybackVolume}%`;
  elements.microphoneButton.textContent = state.microphoneEnabled ? "关闭麦克风" : "开启麦克风";

  if (state.leaseUncertain && ownLease) {
    elements.leaseTitle.textContent = "接管状态待确认";
    elements.leaseDescription.textContent = "控制连接已断开，客服麦克风已关闭。";
    elements.microphoneTitle.textContent = "不可用";
    elements.microphoneDescription.textContent = "等待控制连接恢复后重新确认租约状态。";
  } else if (ownLease) {
    elements.leaseTitle.textContent = "当前客服已接管";
    elements.leaseDescription.textContent = "人工接管已生效，客服麦克风仍需手动开启。";
    elements.microphoneTitle.textContent = state.microphoneEnabled ? "正在发布" : "等待开启";
    elements.microphoneDescription.textContent = state.microphoneEnabled
      ? `客服麦克风正在使用“${voiceModeLabel(state.voiceMode)}”效果发布。`
      : "接管租约有效，Windows 原生客服麦克风保持关闭。";
  } else if (foreignLease) {
    elements.leaseTitle.textContent = `${session.takeover.agentDisplayName} 接管中`;
    elements.leaseDescription.textContent = "当前客服可以查看和听取，但不能发布客服麦克风。";
    elements.microphoneTitle.textContent = "不可用";
    elements.microphoneDescription.textContent = "接管租约由其他客服持有。";
  } else if (unmanagedOwnLease) {
    elements.leaseTitle.textContent = "当前账号接管中";
    elements.leaseDescription.textContent = "接管租约由同一客服账号的另一页面持有。";
    elements.microphoneTitle.textContent = "不可用";
    elements.microphoneDescription.textContent = "请在其他页面释放接管后再继续。";
  } else {
    elements.leaseTitle.textContent = "未接管";
    elements.leaseDescription.textContent = state.nativeAudioAvailable
      ? "当前会话尚未由人工接管。"
      : "Windows ARTC 音频助手未就绪，不能开启客服麦克风。";
    elements.microphoneTitle.textContent = "默认关闭";
    elements.microphoneDescription.textContent = "尚未取得人工接管租约。";
  }

  elements.microphoneIndicator.dataset.active = String(state.microphoneEnabled);
  renderLeaseState();
}


function renderLeaseState() {
  if (!state.lease || state.leaseUncertain) {
    return;
  }
  if (leaseRemainingSeconds() <= 0) {
    elements.microphoneButton.disabled = true;
    elements.microphoneButton.textContent = "租约已过期";
  }
}


async function selectSession(sessionId) {
  if (state.joinedSessionId && state.joinedSessionId !== sessionId) {
    await leaveSession({ reason: "已切换业务会话" });
  }
  state.selectedSessionId = sessionId;
  render();
}


async function joinSelectedSession() {
  const session = selectedSession();
  if (!session) {
    return;
  }
  if (state.joinedSessionId === session.id) {
    return;
  }
  if (state.joinedSessionId) {
    await leaveSession({ reason: "已切换业务会话" });
  }

  elements.joinButton.disabled = true;
  elements.joinButton.textContent = "接入中";
  elements.rtcState.textContent = "正在申请媒体权限";
  state.ignoredMediaUserIds.clear();
  state.robotMediaUserId = `robot-${session.robotId}`;

  let claimAcquired = false;
  try {
    const claimedSession = await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(session.id)}/claim`,
      { method: "POST", body: {} },
    );
    claimAcquired = true;
    upsertSession(claimedSession);
    const auth = await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(session.id)}/viewer-token`,
      { method: "POST", body: {} },
    );
    const rtc = new AgentRtcSession({
      videoElement: elements.remoteVideo,
      onEvent: handleRtcEvent,
    });
    state.rtc = rtc;
    await rtc.connect(auth);
    state.joinedSessionId = session.id;

    await setListening(true);
    applyRobotAudioVolume();
    await sendAgentPlaybackVolume();
    elements.rtcState.textContent = "媒体已连接";
    elements.audioState.textContent = "等待机器人音频";
    showToast("已接入业务会话", "success");
  } catch (error) {
    if (claimAcquired) {
      await apiRequest(
        `/api/robot-sessions/${encodeURIComponent(session.id)}/claim/release`,
        { method: "POST", body: {} },
      ).catch(() => {});
    }
    await state.rtc?.disconnect({ emit: false });
    state.rtc = null;
    state.joinedSessionId = null;
    state.listening = false;
    state.microphoneEnabled = false;
    state.ignoredMediaUserIds.clear();
    state.robotMediaUserId = null;
    elements.rtcState.textContent = "媒体接入失败";
    elements.audioState.textContent = "机器人音频不可用";
    showToast(errorText(error), "danger");
  } finally {
    render();
  }
}


async function setListening(enabled) {
  if (!state.joinedSessionId) {
    return;
  }
  await apiRequest(
    `/api/robot-sessions/${encodeURIComponent(state.joinedSessionId)}/listen`,
    { method: "POST", body: { enabled } },
  );
  state.listening = enabled;
  render();
}


async function toggleListening() {
  try {
    await setListening(!state.listening);
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}


async function acquireTakeover() {
  const session = selectedSession();
  if (!session || state.joinedSessionId !== session.id || state.lease) {
    return;
  }
  elements.takeoverButton.disabled = true;
  try {
    const lease = await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(session.id)}/takeover-lease`,
      {
        method: "POST",
        body: { requestedSeconds: LEASE_SECONDS },
      },
    );
    state.lease = lease;
    state.leaseUncertain = false;
    updateTakeover(session.id, lease);
    await startNativeAudioForLease(session.id);
    scheduleLeaseRenewal();
    showToast("人工接管已开始，客服麦克风仍保持关闭", "success");
    render();
  } catch (error) {
    await stopNativeAudio();
    if (state.lease?.sessionId === session.id) {
      await apiRequest(
        `/api/robot-sessions/${encodeURIComponent(session.id)}/takeover-lease/release`,
        { method: "POST", body: {} },
      ).catch(() => {});
      state.lease = null;
      state.leaseUncertain = false;
      window.clearTimeout(state.leaseRenewTimer);
      updateTakeover(session.id, null);
    }
    showToast(errorText(error), "danger");
    await refreshCatalog({ silent: true });
    render();
  }
}


function scheduleLeaseRenewal() {
  window.clearTimeout(state.leaseRenewTimer);
  if (!state.lease) {
    return;
  }
  const expires = new Date(state.lease.expiresAt).getTime();
  const delay = Number.isFinite(expires)
    ? Math.max(5000, expires - Date.now() - 15000)
    : 30000;
  state.leaseRenewTimer = window.setTimeout(renewTakeover, delay);
}


async function renewTakeover() {
  if (!state.lease?.sessionId) {
    return;
  }
  const sessionId = state.lease.sessionId;
  try {
    const lease = await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(sessionId)}/takeover-lease/renew`,
      {
        method: "POST",
        body: { requestedSeconds: LEASE_SECONDS },
      },
    );
    if (state.lease?.sessionId !== sessionId) {
      return;
    }
    state.lease = lease;
    state.leaseUncertain = false;
    updateTakeover(sessionId, lease);
    scheduleLeaseRenewal();
    render();
  } catch (error) {
    await stopLocalTakeover("接管租约续期失败");
    showToast(`${errorText(error)}；客服麦克风已关闭`, "danger");
  }
}


async function reportTakeoverMicrophone(enabled) {
  const sessionId = state.lease?.sessionId;
  if (!sessionId) {
    throw new Error("没有有效的接管租约");
  }
  const lease = await apiRequest(
    `/api/robot-sessions/${encodeURIComponent(sessionId)}/takeover-lease/microphone`,
    { method: "POST", body: { enabled: Boolean(enabled) } },
  );
  if (state.lease?.sessionId === sessionId) {
    state.lease = { ...state.lease, ...lease };
  }
  updateTakeover(sessionId, lease);
  return lease;
}


async function toggleMicrophone() {
  if (!state.lease || state.leaseUncertain || leaseRemainingSeconds() <= 0) {
    showToast("没有有效的接管租约", "danger");
    return;
  }
  if (!state.rtc || !state.nativeAudioAvailable) {
    showToast("Windows ARTC 音频助手未就绪", "danger");
    return;
  }

  const target = !state.microphoneEnabled;
  elements.microphoneButton.disabled = true;
  try {
    if (!state.nativeAudio?.sessionId) {
      await startNativeAudioForLease(state.lease.sessionId);
    }
    const nativeAudio = requireNativeAudio();
    await nativeAudio.setMicrophone(target);
    await reportTakeoverMicrophone(target);
    state.microphoneEnabled = target;
    render();
  } catch (error) {
    state.microphoneEnabled = false;
    await state.nativeAudio?.setMicrophone(false).catch(() => {});
    showToast(errorText(error), "danger");
    render();
  }
}


async function changeVoiceMode() {
  const nextMode = Number(elements.voiceMode.value);
  if (!(nextMode in state.voiceModeLabels)) {
    return;
  }
  const previousMode = state.voiceMode;
  state.voiceMode = nextMode;
  render();
  if (!state.nativeAudio?.sessionId) {
    return;
  }
  try {
    await state.nativeAudio.setVoiceMode(
      nextMode,
      nextMode === 100 ? state.comboVoice : null,
    );
    showToast(`音色改变已切换为“${voiceModeLabel(nextMode)}”`, "success");
  } catch (error) {
    state.voiceMode = previousMode;
    elements.voiceMode.value = String(previousMode);
    showToast(errorText(error), "danger");
    render();
  }
}


async function releaseTakeover({ silent = false } = {}) {
  if (!state.lease) {
    return;
  }
  const sessionId = state.lease.sessionId;
  await stopLocalMicrophone();
  await stopNativeAudio();
  try {
    await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(sessionId)}/takeover-lease/release`,
      { method: "POST", body: {} },
    );
    updateTakeover(sessionId, null);
    if (!silent) {
      showToast("人工接管已释放", "success");
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      updateTakeover(sessionId, null);
    } else {
      if (!silent) {
        showToast(errorText(error), "danger");
      }
    }
  } finally {
    if (state.lease?.sessionId === sessionId) {
      state.lease = null;
      state.leaseUncertain = false;
    }
    window.clearTimeout(state.leaseRenewTimer);
    render();
  }
}


async function stopLocalTakeover(reason) {
  await stopLocalMicrophone();
  await stopNativeAudio();
  state.lease = null;
  state.leaseUncertain = false;
  window.clearTimeout(state.leaseRenewTimer);
  if (reason) {
    showToast(reason, "warning");
  }
  render();
}


async function stopLocalMicrophone() {
  if (state.nativeAudio?.sessionId && state.microphoneEnabled) {
    try {
      await state.nativeAudio.setMicrophone(false);
    } catch (_error) {
      // 离会或网络断开时仍继续清理本地可见状态。
    }
    try {
      await reportTakeoverMicrophone(false);
    } catch (_error) {
      // 控制面断线时由租约释放或过期清理服务端麦克风状态。
    }
  }
  state.microphoneEnabled = false;
}


async function leaveSession({
  silent = false,
  releaseTakeover: shouldRelease = true,
  releaseClaim = true,
  reason = "",
} = {}) {
  if (state.leaving) {
    return;
  }
  const sessionId = state.joinedSessionId;
  state.leaving = true;
  state.leavingSessionId = sessionId;
  render();
  try {
    await stopLocalMicrophone();
    if (shouldRelease && state.lease) {
      await releaseTakeover({ silent: true });
    } else {
      await stopNativeAudio();
      state.lease = null;
      state.leaseUncertain = false;
      window.clearTimeout(state.leaseRenewTimer);
    }

    if (sessionId && state.listening) {
      try {
        await apiRequest(
          `/api/robot-sessions/${encodeURIComponent(sessionId)}/listen`,
          { method: "POST", body: { enabled: false } },
        );
      } catch (_error) {
        // WebSocket 断线清理会兜底停止机器人麦克风请求。
      }
    }

    if (state.rtc) {
      await state.rtc.disconnect({ emit: false });
    }
    if (sessionId && releaseClaim) {
      try {
        await apiRequest(
          `/api/robot-sessions/${encodeURIComponent(sessionId)}/claim/release`,
          { method: "POST", body: {} },
        );
      } catch (_error) {
        // 控制面断线时由服务端 WebSocket 清理兜底释放认领。
      }
    }
    state.rtc = null;
    state.joinedSessionId = null;
    state.listening = false;
    state.microphoneEnabled = false;
    state.ignoredMediaUserIds.clear();
    state.robotMediaUserId = null;
    elements.rtcState.textContent = "媒体未接入";
    elements.audioState.textContent = "机器人音频等待中";
    elements.audioResumeButton.hidden = true;
    elements.remoteVideo.hidden = true;
    elements.videoFrame.classList.remove("has-video");
    if (!silent) {
      showToast(reason || "已离开业务会话", "neutral");
    }
    render();
  } finally {
    state.leaving = false;
    state.leavingSessionId = null;
    render();
  }
}


async function endCurrentSession() {
  const sessionId = state.joinedSessionId;
  if (!sessionId) {
    return;
  }
  if (!window.confirm("结束会话后机器人将退出当前 ARTC 频道，确认继续？")) {
    return;
  }
  elements.endSessionButton.disabled = true;
  state.endingSessionId = sessionId;
  render();
  try {
    await stopLocalMicrophone();
    await stopNativeAudio();
    await apiRequest(
      `/api/robot-sessions/${encodeURIComponent(sessionId)}/end`,
      { method: "POST", body: {} },
    );
    state.sessions = state.sessions.filter(
      (session) => session.id !== sessionId,
    );
    if (state.selectedSessionId === sessionId) {
      state.selectedSessionId = null;
    }
    if (state.joinedSessionId === sessionId) {
      await leaveSession({
        silent: true,
        releaseTakeover: false,
        releaseClaim: false,
      });
    }
    showToast("业务会话已结束，机器人正在退出频道", "success");
    render();
  } catch (error) {
    elements.endSessionButton.disabled = false;
    showToast(errorText(error), "danger");
  } finally {
    state.endingSessionId = null;
    render();
  }
}


async function terminateRemoteRobot() {
  const session = sessionById(state.joinedSessionId);
  if (!session || state.terminatingRobot) {
    return;
  }
  const robot = robotForSession(session);
  const robotName = robot?.displayName || session.robotDisplayName || "机器人";
  if (
    !window.confirm(
      `将立即结束远端“${robotName}”进程并退出当前频道，确认继续？`,
    )
  ) {
    return;
  }

  state.terminatingRobot = true;
  render();
  try {
    await stopLocalMicrophone();
    await stopNativeAudio();
    const result = await apiRequest(
      `/api/robots/${encodeURIComponent(session.robotId)}/terminate`,
      { method: "POST", body: {} },
    );
    if (!result.delivered) {
      throw new Error("机器人控制连接不在线");
    }
    showToast("退出命令已送达，正在等待机器人离线", "success");
    await leaveSession({
      silent: true,
      releaseTakeover: false,
      releaseClaim: false,
    });
  } catch (error) {
    showToast(`结束机器人进程失败：${errorText(error)}`, "danger");
  } finally {
    state.terminatingRobot = false;
    render();
  }
}


function handleRtcEvent(event) {
  switch (event.type) {
    case "connected":
      elements.rtcState.textContent = "媒体已连接";
      break;
    case "disconnected":
      elements.rtcState.textContent = "媒体已断开";
      break;
    case "connection":
      elements.rtcState.textContent = event.label;
      if (event.state === 5) {
        showToast("ARTC 媒体连接失败", "danger");
      }
      break;
    case "remote-user":
      if (state.ignoredMediaUserIds.has(event.userId)) {
        if (event.online) {
          state.rtc?.muteRemoteAudio(event.userId, true);
        }
        break;
      }
      if (event.online && event.userId === state.robotMediaUserId) {
        applyRobotAudioVolume();
      }
      if (!event.online) {
        elements.audioState.textContent = "远端媒体已离开";
      }
      break;
    case "audio-subscription":
      if (state.ignoredMediaUserIds.has(event.userId)) {
        state.rtc?.muteRemoteAudio(event.userId, true);
        break;
      }
      if (event.userId === state.robotMediaUserId) {
        applyRobotAudioVolume();
      }
      if (event.newState === 3) {
        elements.audioState.textContent = "机器人音频已接入";
        elements.audioResumeButton.hidden = true;
      } else if (event.newState === 1) {
        elements.audioState.textContent = "机器人音频未订阅";
      } else {
        elements.audioState.textContent = `机器人音频${event.label}`;
      }
      break;
    case "audio-autoplay-blocked":
      elements.audioState.textContent = "等待恢复声音";
      elements.audioResumeButton.hidden = false;
      break;
    case "audio-resume-requested":
      elements.audioResumeButton.hidden = true;
      break;
    case "video-subscription":
      if (event.newState === 2) {
        elements.videoEmptyText.textContent = "正在接收机器人视频";
      }
      break;
    case "remote-video":
      elements.videoFrame.classList.toggle("has-video", event.active);
      elements.videoEmpty.hidden = event.active;
      elements.remoteVideo.hidden = !event.active;
      break;
    case "microphone":
      state.microphoneEnabled = Boolean(event.enabled);
      render();
      break;
    case "token-expiring":
      void refreshRtcToken();
      break;
    case "token-expired":
      void refreshRtcToken({ expired: true });
      break;
    case "token-refreshed":
      showToast("ARTC 媒体凭证已刷新", "success");
      break;
    case "error":
      showToast(event.message || "ARTC 媒体异常", "danger");
      break;
    default:
      break;
  }
}


async function refreshRtcToken({ expired = false } = {}) {
  if (!state.joinedSessionId || !state.rtc) {
    return;
  }
  if (state.tokenRefresh) {
    return state.tokenRefresh;
  }
  const sessionId = state.joinedSessionId;
  state.tokenRefresh = (async () => {
    try {
      const auth = await apiRequest(
        `/api/robot-sessions/${encodeURIComponent(sessionId)}/viewer-token`,
        { method: "POST", body: {} },
      );
      if (state.joinedSessionId !== sessionId) {
        return;
      }
      await state.rtc.refreshToken(auth);
    } catch (error) {
      showToast(`媒体凭证刷新失败：${errorText(error)}`, "danger");
      if (expired) {
        await leaveSession({ silent: true, reason: "媒体凭证已过期" });
      }
    } finally {
      state.tokenRefresh = null;
    }
  })();
  return state.tokenRefresh;
}


async function connectControlSocket() {
  if (!state.accessToken || state.socket || state.loggingOut) {
    return;
  }
  window.clearTimeout(state.socketReconnectTimer);
  setControlState("控制连接中", "connecting");

  try {
    const ticket = await apiRequest("/api/auth/ws-ticket");
    if (!state.accessToken || state.socket) {
      return;
    }
    const backend = new URL(state.backendUrl);
    const scheme = backend.protocol === "https:" ? "wss:" : "ws:";
    const backendPath = backend.pathname.replace(/\/$/, "");
    const socket = new WebSocket(
      `${scheme}//${backend.host}${backendPath}/ws?ticket=${encodeURIComponent(ticket.ticket)}`,
    );
    state.socket = socket;

    socket.addEventListener("open", () => {
      setControlState("控制已连接", "success");
      void refreshCatalog({ silent: true });
      window.clearInterval(state.socketPingTimer);
      state.socketPingTimer = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }));
        }
      }, PING_INTERVAL_MS);
    });

    socket.addEventListener("message", (message) => {
      try {
        handleControlMessage(JSON.parse(message.data));
      } catch (_error) {
        // 忽略无法识别的控制消息，轮询会恢复状态。
      }
    });

    socket.addEventListener("close", (closeEvent) => {
      window.clearInterval(state.socketPingTimer);
      if (state.socket === socket) {
        state.socket = null;
      }
      if (state.loggingOut || !state.accessToken) {
        return;
      }
      if (closeEvent.code === 4403) {
        void forceLogout("客服账号已停用");
        return;
      }
      setControlState("控制已断开", "danger");
      void handleControlInterruption();
      scheduleControlReconnect();
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await forceLogout("登录已失效，请重新登录");
      return;
    }
    setControlState("控制连接失败", "danger");
    scheduleControlReconnect();
  }
}


function scheduleControlReconnect() {
  if (!state.accessToken || state.loggingOut || state.socketReconnectTimer) {
    return;
  }
  state.socketReconnectTimer = window.setTimeout(() => {
    state.socketReconnectTimer = null;
    void connectControlSocket();
  }, RECONNECT_DELAY_MS);
}


function handleControlMessage(message) {
  switch (message.type) {
    case "snapshot":
      applySessions(Array.isArray(message.sessions) ? message.sessions : []);
      state.robots = new Map(
        (Array.isArray(message.robots) ? message.robots : []).map((robot) => [robot.id, robot]),
      );
      reconcileUncertainLease();
      const joinedSession = state.joinedSessionId
        ? sessionById(state.joinedSessionId)
        : null;
      if (
        joinedSession
        && joinedSession.claim?.agentId !== state.principal?.id
        && !state.leaving
      ) {
        void leaveSession({
          silent: true,
          releaseTakeover: false,
          reason: "会话占用已释放",
        });
      }
      render();
      break;
    case "session_started":
      if (message.session) {
        upsertSession(message.session);
        render();
      }
      break;
    case "session_claimed":
    case "session_released":
      if (message.session) {
        upsertSession(message.session);
        if (
          state.joinedSessionId === message.session.id
          && !message.session.claim
          && !state.leaving
        ) {
          void leaveSession({
            silent: true,
            releaseTakeover: false,
            reason: "会话占用已释放",
          });
        }
        render();
      }
      break;
    case "session_ended":
      handleSessionEnded(message.session?.id);
      break;
    case "agent_takeover":
    case "takeover_microphone_changed":
      if (message.lease) {
        if (state.lease?.id === message.lease.id) {
          state.lease = { ...state.lease, ...message.lease };
        }
        updateTakeover(message.sessionId, message.lease);
        render();
      }
      break;
    case "takeover_released":
    case "takeover_expired":
      if (
        state.lease?.sessionId === message.sessionId
        && state.lease?.id === message.lease?.id
      ) {
        void stopNativeAudio();
        state.lease = null;
        state.leaseUncertain = false;
        window.clearTimeout(state.leaseRenewTimer);
      }
      updateTakeover(message.sessionId, null);
      render();
      break;
    case "listen_changed":
      if (message.agentId === state.principal?.id && state.joinedSessionId === message.sessionId) {
        state.listening = Boolean(message.enabled);
        render();
      }
      break;
    case "media_state":
      updateRobotMedia(message);
      render();
      break;
    case "robot_status":
      updateRobotStatus(message);
      render();
      break;
    case "robot_updated":
      if (message.robot?.id) {
        state.robots.set(message.robot.id, message.robot);
        for (const session of state.sessions) {
          if (session.robotId === message.robot.id) {
            session.robotDisplayName = message.robot.displayName;
          }
        }
        render();
      }
      break;
    case "robot_deleted":
      if (message.robotId) {
        state.robots.delete(message.robotId);
        state.sessions = state.sessions.filter(
          (session) => session.robotId !== message.robotId,
        );
        if (
          state.joinedSessionId
          && !sessionById(state.joinedSessionId)
        ) {
          void leaveSession({
            silent: true,
            releaseTakeover: false,
            reason: "机器人条目已删除",
          });
        }
        render();
      }
      break;
    case "error":
      showToast(message.message || "控制消息处理失败", "danger");
      break;
    default:
      break;
  }
}


async function handleControlInterruption() {
  await stopLocalMicrophone();
  await stopNativeAudio();
  if (state.lease) {
    state.leaseUncertain = true;
    window.clearTimeout(state.leaseRenewTimer);
    showToast("控制连接中断，客服麦克风已关闭", "warning");
  }
  render();
}


function updateRobotMedia(message) {
  const robot = state.robots.get(message.robotId);
  if (!robot) {
    return;
  }
  robot.media = {
    sessionId: message.sessionId,
    microphonePublished: Boolean(message.microphonePublished),
    cameraPublished: Boolean(message.cameraPublished),
    automaticSpeechPaused: Boolean(message.automaticSpeechPaused),
    updatedAt: message.updatedAt,
  };
}


function updateRobotStatus(message) {
  const robot = state.robots.get(message.robotId);
  if (!robot) {
    return;
  }
  robot.status = message.to || robot.status;
  robot.lastSeenAt = message.lastSeenAt || robot.lastSeenAt;
  for (const session of state.sessions) {
    if (session.robotId === message.robotId) {
      session.robotStatus = robot.status;
    }
  }
}


function handleSessionEnded(sessionId) {
  if (!sessionId) {
    return;
  }
  state.sessions = state.sessions.filter((session) => session.id !== sessionId);
  if (state.joinedSessionId === sessionId) {
    void leaveSession({
      silent: true,
      releaseTakeover: false,
      reason: "业务会话已结束",
    });
  }
  if (state.selectedSessionId === sessionId) {
    state.selectedSessionId = null;
  }
  showToast("业务会话已结束", "neutral");
  render();
}


function startPolling() {
  window.clearInterval(state.catalogTimer);
  window.clearInterval(state.clockTimer);
  state.catalogTimer = window.setInterval(() => {
    void refreshCatalog({ silent: true });
  }, CATALOG_REFRESH_MS);
  state.clockTimer = window.setInterval(() => {
    renderLeaseState();
  }, 1000);
}


function stopTimers() {
  stopNativeHeartbeat();
  window.clearTimeout(state.talkTokenRefreshTimer);
  window.clearTimeout(state.socketReconnectTimer);
  window.clearInterval(state.socketPingTimer);
  window.clearInterval(state.catalogTimer);
  window.clearInterval(state.clockTimer);
  window.clearTimeout(state.leaseRenewTimer);
  state.socketReconnectTimer = null;
  state.socketPingTimer = null;
  state.catalogTimer = null;
  state.clockTimer = null;
  state.leaseRenewTimer = null;
  state.talkTokenRefreshTimer = null;
  state.playbackVolumeTimer = null;
  state.comboVoiceTimer = null;
}


async function forceLogout(message) {
  if (state.loggingOut) {
    return;
  }
  state.loggingOut = true;
  try {
    await leaveSession({ silent: true, releaseTakeover: true });
  } finally {
    stopTimers();
    if (state.socket) {
      const socket = state.socket;
      state.socket = null;
      socket.close(1000, "logout");
    }
    state.accessToken = "";
    state.principal = null;
    state.sessions = [];
    state.robots = new Map();
    state.selectedSessionId = null;
    state.joinedSessionId = null;
    state.listening = false;
    state.lease = null;
    state.leaseUncertain = false;
    state.microphoneEnabled = false;
    state.ignoredMediaUserIds.clear();
    elements.agentName.textContent = "";
    elements.sessionList.replaceChildren();
    elements.sessionEmpty.hidden = false;
    render();
    showLogin();
    setLoginError(message);
    state.loggingOut = false;
  }
}


async function logout() {
  const token = state.accessToken;
  state.loggingOut = true;
  try {
    await leaveSession({ silent: true });
    if (token) {
      try {
        await apiRequest("/api/auth/logout", { method: "POST", body: {} });
      } catch (_error) {
        // 本地登出不依赖服务端响应。
      }
    }
  } finally {
    stopTimers();
    if (state.socket) {
      const socket = state.socket;
      state.socket = null;
      socket.close(1000, "logout");
    }
    state.accessToken = "";
    state.principal = null;
    state.sessions = [];
    state.robots = new Map();
    state.selectedSessionId = null;
    state.joinedSessionId = null;
    state.listening = false;
    state.lease = null;
    state.leaseUncertain = false;
    state.microphoneEnabled = false;
    state.ignoredMediaUserIds.clear();
    elements.agentName.textContent = "";
    elements.sessionList.replaceChildren();
    elements.sessionEmpty.hidden = false;
    render();
    showLogin();
    state.loggingOut = false;
  }
}


function sendPageExitRequests() {
  if (!state.accessToken) {
    return;
  }
  stopNativeHeartbeat();
  void state.nativeAudio?.stop({ keepalive: true });
  if (state.lease?.sessionId) {
    void apiRequest(
      `/api/robot-sessions/${encodeURIComponent(state.lease.sessionId)}/takeover-lease/release`,
      { method: "POST", body: {}, keepalive: true },
    ).catch(() => {});
  }
  if (state.joinedSessionId && state.listening) {
    void apiRequest(
      `/api/robot-sessions/${encodeURIComponent(state.joinedSessionId)}/listen`,
      { method: "POST", body: { enabled: false }, keepalive: true },
    ).catch(() => {});
  }
  state.socket?.close(1000, "pagehide");
}


function bindEvents() {
  elements.loginForm.addEventListener("submit", handleLogin);
  elements.logoutButton.addEventListener("click", () => void logout());
  elements.refreshSessionsButton.addEventListener("click", () => void refreshCatalog());
  elements.joinButton.addEventListener("click", () => void joinSelectedSession());
  elements.leaveButton.addEventListener("click", () => void leaveSession());
  elements.endSessionButton.addEventListener("click", () => void endCurrentSession());
  elements.terminateRobotButton.addEventListener("click", () => void terminateRemoteRobot());
  elements.listenButton.addEventListener("click", () => void toggleListening());
  elements.takeoverButton.addEventListener("click", () => void acquireTakeover());
  elements.releaseButton.addEventListener("click", () => void releaseTakeover());
  elements.microphoneButton.addEventListener("click", () => void toggleMicrophone());
  elements.voiceMode.addEventListener("change", () => void changeVoiceMode());
  elements.robotAudioVolume.addEventListener("input", (event) => {
    updateRobotAudioVolume(event.target.value);
  });
  elements.agentPlaybackVolume.addEventListener("input", (event) => {
    updateAgentPlaybackVolume(event.target.value);
  });
  for (const control of [
    elements.comboPitch,
    elements.comboLowGain,
    elements.comboMidGain,
    elements.comboHighGain,
  ]) {
    control.addEventListener("input", handleComboVoiceInput);
  }
  for (const control of [
    elements.comboBaseVoice,
    elements.comboReverb,
    elements.comboBeautify,
  ]) {
    control.addEventListener("change", handleComboVoiceInput);
  }
  elements.audioResumeButton.addEventListener("click", () => {
    void state.rtc?.resumeRemoteAudio();
  });
  window.addEventListener("pagehide", sendPageExitRequests);
}


async function loadAgentConfig() {
  const response = await fetch("/agent-config.json", {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const payload = await response.json();
  const backend = new URL(String(payload.backendUrl || ""));
  if (!["http:", "https:"].includes(backend.protocol)) {
    throw new Error("backendUrl 必须使用 http:// 或 https://");
  }
  state.backendUrl = backend.toString().replace(/\/$/, "");
  state.nativeAudioAvailable = Boolean(payload.nativeAudioAvailable);
  state.agentControlToken = String(payload.agentControlToken || "");
  const voiceModes = Array.isArray(payload.voiceModes) ? payload.voiceModes : [];
  if (voiceModes.length === 0) {
    throw new Error("客服工作台没有可用的音色改变列表");
  }
  const labels = {};
  const values = new Set();
  const options = voiceModes.map((mode) => {
    const value = Number(mode?.value);
    const label = String(mode?.label || "").trim();
    const sdkName = String(mode?.sdkName || "").trim();
    if (!Number.isInteger(value) || value < 0 || !label || values.has(value)) {
      throw new Error("客服工作台音色改变列表无效");
    }
    values.add(value);
    labels[value] = label;
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = label;
    if (sdkName) {
      option.title = sdkName;
    }
    return option;
  });
  state.voiceModeLabels = labels;
  if (!(state.voiceMode in labels)) {
    state.voiceMode = 0;
  }
  elements.voiceMode.replaceChildren(...options);
  elements.voiceMode.value = String(state.voiceMode);

  const baseVoiceModes = voiceModes.filter(
    (mode) => Number(mode.value) >= 0 && Number(mode.value) <= 11,
  );
  const reverbModes = Array.isArray(payload.reverbModes) ? payload.reverbModes : [];
  const beautifyModes = Array.isArray(payload.beautifyModes) ? payload.beautifyModes : [];
  const comboDefaults = payload.comboVoiceDefaults;
  if (
    baseVoiceModes.length === 0
    || reverbModes.length === 0
    || beautifyModes.length === 0
    || !comboDefaults
  ) {
    throw new Error("客服工作台组合音色配置无效");
  }
  elements.comboBaseVoice.replaceChildren(
    ...baseVoiceModes.map((mode) => {
      const option = document.createElement("option");
      option.value = String(mode.value);
      option.textContent = String(mode.label);
      return option;
    }),
  );
  const populateModeSelect = (select, modes) => {
    select.replaceChildren(
      ...modes.map((mode) => {
        const option = document.createElement("option");
        option.value = String(mode.value);
        option.textContent = String(mode.label);
        return option;
      }),
    );
  };
  populateModeSelect(elements.comboReverb, reverbModes);
  populateModeSelect(elements.comboBeautify, beautifyModes);
  state.comboVoiceDefaults = {
    baseVoiceMode: Number(comboDefaults.baseVoiceMode),
    pitch: Number(comboDefaults.pitch),
    reverbMode: Number(comboDefaults.reverbMode),
    beautifyMode: Number(comboDefaults.beautifyMode),
    lowGain: Number(comboDefaults.lowGain),
    midGain: Number(comboDefaults.midGain),
    highGain: Number(comboDefaults.highGain),
  };
  state.comboVoiceLabels = {
    reverb: Object.fromEntries(
      reverbModes.map((mode) => [Number(mode.value), String(mode.label)]),
    ),
    beautify: Object.fromEntries(
      beautifyModes.map((mode) => [Number(mode.value), String(mode.label)]),
    ),
  };
  setComboVoiceControls(state.comboVoiceDefaults);
  state.configReady = true;
  setLoginBusy(false);
}


async function initialize() {
  elements.remoteVideo.hidden = true;
  bindEvents();
  setControlState("控制未连接", "neutral");
  render();
  try {
    await loadAgentConfig();
  } catch (error) {
    state.configReady = false;
    elements.loginButton.disabled = true;
    setLoginError(`无法加载客服工作台配置：${errorText(error)}`);
  }
}


void initialize();
