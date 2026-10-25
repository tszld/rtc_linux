import { AgentRtcSession } from "./rtc.js";


const ROBOT_STATUS_LABELS = {
  online: "在线",
  degraded: "连接波动",
  offline: "离线",
};

const LEASE_SECONDS = 60;
const RECONNECT_DELAY_MS = 2000;
const PING_INTERVAL_MS = 15000;
const CATALOG_REFRESH_MS = 4000;


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
  leaseTitle: document.querySelector("#leaseTitle"),
  leaseTimer: document.querySelector("#leaseTimer"),
  leaseDescription: document.querySelector("#leaseDescription"),
  microphoneTitle: document.querySelector("#microphoneTitle"),
  microphoneDescription: document.querySelector("#microphoneDescription"),
  microphoneIndicator: document.querySelector("#microphoneIndicator"),
  microphoneButton: document.querySelector("#microphoneButton"),
  takeoverButton: document.querySelector("#takeoverButton"),
  releaseButton: document.querySelector("#releaseButton"),
  joinButton: document.querySelector("#joinButton"),
  leaveButton: document.querySelector("#leaveButton"),
  endSessionButton: document.querySelector("#endSessionButton"),
  listenButton: document.querySelector("#listenButton"),
  toastRegion: document.querySelector("#toastRegion"),
};


const state = {
  backendUrl: "",
  configReady: false,
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

  elements.joinButton.hidden = false;
  elements.joinButton.disabled = isJoined || claimOwnedByOther;
  elements.joinButton.textContent = isJoined
    ? "已接入"
    : claimOwnedByOther
      ? `${session.claim.agentDisplayName || "其他客服"}正在处理`
      : "开始处理";
  elements.leaveButton.disabled = !isJoined;
  elements.endSessionButton.disabled = !isJoined;
  elements.listenButton.disabled = !isJoined;
  elements.listenButton.textContent = state.listening ? "停止听取" : "开始听取";

  elements.takeoverButton.disabled = (
    !isJoined
    || !claimOwnedByMe
    || Boolean(session.takeover)
    || Boolean(state.lease)
  );
  elements.releaseButton.disabled = !ownLease || state.leaseUncertain;
  elements.microphoneButton.disabled = (
    !ownLease
    || state.leaseUncertain
    || leaseRemainingSeconds() <= 0
  );
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
      ? "客服麦克风正在向当前频道发布。"
      : "接管租约有效，客服麦克风保持关闭。";
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
    elements.leaseDescription.textContent = "当前会话尚未由人工接管。";
    elements.microphoneTitle.textContent = "默认关闭";
    elements.microphoneDescription.textContent = "尚未取得人工接管租约。";
  }

  elements.microphoneIndicator.dataset.active = String(state.microphoneEnabled);
  renderLeaseTimer();
}


function renderLeaseTimer() {
  if (!state.lease || state.leaseUncertain) {
    elements.leaseTimer.textContent = "--";
    return;
  }
  const seconds = leaseRemainingSeconds();
  elements.leaseTimer.textContent = `${seconds}s`;
  if (seconds <= 0) {
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
    scheduleLeaseRenewal();
    showToast("人工接管已开始，客服麦克风仍保持关闭", "success");
    render();
  } catch (error) {
    showToast(errorText(error), "danger");
    await refreshCatalog({ silent: true });
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
  if (!state.rtc) {
    showToast("媒体会话未接入", "danger");
    return;
  }

  const target = !state.microphoneEnabled;
  elements.microphoneButton.disabled = true;
  try {
    await state.rtc.setMicrophone(target);
    await reportTakeoverMicrophone(target);
    state.microphoneEnabled = target;
    render();
  } catch (error) {
    state.microphoneEnabled = false;
    await state.rtc.setMicrophone(false).catch(() => {});
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
  state.lease = null;
  state.leaseUncertain = false;
  window.clearTimeout(state.leaseRenewTimer);
  if (reason) {
    showToast(reason, "warning");
  }
  render();
}


async function stopLocalMicrophone() {
  if (state.rtc && state.microphoneEnabled) {
    try {
      await state.rtc.setMicrophone(false);
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
      if (!event.online) {
        elements.audioState.textContent = "远端媒体已离开";
      }
      break;
    case "audio-subscription":
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
        void stopLocalMicrophone();
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
    renderLeaseTimer();
  }, 1000);
}


function stopTimers() {
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
  if (state.microphoneEnabled) {
    void state.rtc?.setMicrophone(false);
  }
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
  elements.listenButton.addEventListener("click", () => void toggleListening());
  elements.takeoverButton.addEventListener("click", () => void acquireTakeover());
  elements.releaseButton.addEventListener("click", () => void releaseTakeover());
  elements.microphoneButton.addEventListener("click", () => void toggleMicrophone());
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
