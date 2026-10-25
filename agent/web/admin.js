const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = {
  backendUrl: "",
  token: "",
  principal: null,
  robots: [],
  agents: [],
  scenarios: [],
  audit: [],
  secretText: "",
  refreshTimer: null,
  refreshing: false,
};

const elements = {
  loginView: $("#adminLoginView"),
  app: $("#adminApp"),
  loginForm: $("#adminLoginForm"),
  username: $("#adminUsername"),
  password: $("#adminPassword"),
  loginButton: $("#adminLoginButton"),
  loginError: $("#adminLoginError"),
  adminName: $("#adminName"),
  logoutButton: $("#adminLogoutButton"),
  refreshButton: $("#refreshAdminButton"),
  createRobotForm: $("#createRobotForm"),
  robotDisplayName: $("#robotDisplayName"),
  robotTableBody: $("#robotTableBody"),
  createAgentForm: $("#createAgentForm"),
  agentUsername: $("#agentUsername"),
  agentDisplayName: $("#agentDisplayName"),
  agentPassword: $("#agentPassword"),
  agentTableBody: $("#agentTableBody"),
  auditTableBody: $("#auditTableBody"),
  secretDialog: $("#secretDialog"),
  secretEyebrow: $("#secretEyebrow"),
  secretTitle: $("#secretTitle"),
  secretMessage: $("#secretMessage"),
  secretLabelOne: $("#secretLabelOne"),
  secretValueOne: $("#secretValueOne"),
  secretLabelTwo: $("#secretLabelTwo"),
  secretValueTwo: $("#secretValueTwo"),
  copySecretButton: $("#copySecretButton"),
  toastRegion: $("#adminToastRegion"),
};

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function errorText(error) {
  return error instanceof Error ? error.message : String(error || "操作失败");
}

async function api(path, { method = "GET", body = null } = {}) {
  if (!state.backendUrl) {
    throw new ApiError("控制端地址尚未加载");
  }
  const headers = { Accept: "application/json" };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
  }
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  let response;
  try {
    response = await fetch(`${state.backendUrl}${path}`, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiError(`无法连接业务控制端：${errorText(error)}`);
  }
  let payload = null;
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    throw new ApiError(payload?.detail || `HTTP ${response.status}`, response.status);
  }
  return payload;
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

function setLoginBusy(busy) {
  elements.loginButton.disabled = busy;
  elements.loginButton.textContent = busy ? "正在登录" : "登录";
  elements.username.disabled = busy;
  elements.password.disabled = busy;
}

function setLoginError(message = "") {
  elements.loginError.textContent = message;
  elements.loginError.hidden = !message;
}

function formatTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
}

function createCell(text, className = "") {
  const cell = document.createElement("td");
  cell.textContent = text;
  if (className) {
    cell.className = className;
  }
  return cell;
}

function createActionButton(label, action, tone = "secondary") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button button-${tone}`;
  button.textContent = label;
  button.addEventListener("click", action);
  return button;
}

function emptyTableRow(colspan, text) {
  const row = document.createElement("tr");
  const cell = createCell(text, "table-empty");
  cell.colSpan = colspan;
  row.append(cell);
  return row;
}

function defaultScenarioId() {
  return state.scenarios.find((scenario) => scenario.enabled)?.id || "";
}

async function ensureDefaultScenario() {
  const existing = defaultScenarioId();
  if (existing) {
    return existing;
  }
  const scenario = await api("/api/admin/scenarios", {
    method: "POST",
    body: { name: "默认场景" },
  });
  state.scenarios = [scenario];
  return scenario.id;
}

function renderRobots() {
  elements.robotTableBody.replaceChildren();
  if (!state.robots.length) {
    elements.robotTableBody.append(emptyTableRow(5, "暂无机器人，启动机器人端后会自动注册"));
    return;
  }
  for (const robot of state.robots) {
    const row = document.createElement("tr");
    row.append(
      createCell(robot.displayName || "-"),
      createCell(robot.id || "-", "mono"),
    );

    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `inline-status ${robot.enabled ? "good" : "bad"}`;
    status.textContent = robot.enabled ? robot.status : "已停用";
    statusCell.append(status);
    row.append(statusCell);

    const approvalCell = document.createElement("td");
    const approval = document.createElement("span");
    approval.className = `inline-status ${robot.approved ? "good" : "warn"}`;
    approval.textContent = robot.approved ? "已审批" : "待审批";
    approvalCell.append(approval);
    row.append(approvalCell);

    const actionsCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "table-actions";
    if (!robot.approved) {
      actions.append(
        createActionButton("通过", () => robotAction(robot.id, "approve"), "primary"),
        createActionButton("拒绝", () => robotAction(robot.id, "reject"), "danger"),
      );
    } else {
      actions.append(
        createActionButton("撤销审批", () => robotAction(robot.id, "reject"), "danger"),
      );
    }
    actions.append(
      createActionButton("重命名", () => renameRobot(robot), "secondary"),
      createActionButton(
        robot.enabled ? "停用" : "启用",
        () => robotAction(robot.id, robot.enabled ? "disable" : "enable"),
        robot.enabled ? "danger" : "secondary",
      ),
      createActionButton("轮换凭证", () => rotateRobotCredential(robot), "secondary"),
      createActionButton("删除", () => deleteRobot(robot), "danger"),
    );
    actionsCell.append(actions);
    row.append(actionsCell);
    elements.robotTableBody.append(row);
  }
}

function renderAgents() {
  elements.agentTableBody.replaceChildren();
  if (!state.agents.length) {
    elements.agentTableBody.append(emptyTableRow(5, "暂无客服账号"));
    return;
  }
  for (const agent of state.agents) {
    const row = document.createElement("tr");
    row.append(
      createCell(agent.username || "-"),
      createCell(agent.displayName || "-"),
      createCell(agent.role === "admin" ? "管理员" : "客服"),
    );
    const stateCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `inline-status ${agent.enabled ? "good" : "bad"}`;
    status.textContent = agent.enabled ? "启用" : "停用";
    stateCell.append(status);
    row.append(stateCell);

    const actionsCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "table-actions";
    if (agent.role === "agent") {
      actions.append(
        createActionButton(
          agent.enabled ? "停用" : "启用",
          () => agentAction(agent, agent.enabled ? "disable" : "enable"),
          agent.enabled ? "danger" : "secondary",
        ),
      );
    }
    actionsCell.append(actions);
    row.append(actionsCell);
    elements.agentTableBody.append(row);
  }
}

function renderAudit() {
  elements.auditTableBody.replaceChildren();
  if (!state.audit.length) {
    elements.auditTableBody.append(emptyTableRow(4, "暂无审计记录"));
    return;
  }
  for (const event of state.audit) {
    const row = document.createElement("tr");
    row.append(
      createCell(formatTime(event.createdAt)),
      createCell(event.action || "-"),
      createCell(event.targetType || "-"),
      createCell(event.targetId || "-", "mono"),
    );
    elements.auditTableBody.append(row);
  }
}

function render() {
  renderRobots();
  renderAgents();
  renderAudit();
}

async function refreshAll({ quiet = false } = {}) {
  if (!state.token || state.refreshing) {
    return;
  }
  state.refreshing = true;
  try {
    const [robots, agents, scenarios, audit] = await Promise.all([
      api("/api/admin/robots"),
      api("/api/admin/agents"),
      api("/api/admin/scenarios"),
      api("/api/admin/audit?limit=100"),
    ]);
    state.robots = Array.isArray(robots) ? robots : [];
    state.agents = Array.isArray(agents) ? agents : [];
    state.scenarios = Array.isArray(scenarios) ? scenarios : [];
    state.audit = Array.isArray(audit) ? audit : [];
    render();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await logout();
      return;
    }
    if (!quiet) {
      showToast(errorText(error), "danger");
    }
  } finally {
    state.refreshing = false;
  }
}

async function login(event) {
  event.preventDefault();
  setLoginError("");
  setLoginBusy(true);
  try {
    const payload = await api("/api/admin/login", {
      method: "POST",
      body: {
        username: elements.username.value.trim(),
        password: elements.password.value,
      },
    });
    if (payload.principal?.role !== "admin") {
      throw new ApiError("该账号没有管理员权限", 403);
    }
    state.token = payload.accessToken;
    state.principal = payload.principal;
    elements.adminName.textContent = state.principal.displayName || state.principal.id;
    elements.password.value = "";
    elements.loginView.hidden = true;
    elements.app.hidden = false;
    await refreshAll();
    state.refreshTimer = window.setInterval(() => {
      void refreshAll({ quiet: true });
    }, 5000);
  } catch (error) {
    setLoginError(errorText(error));
  } finally {
    setLoginBusy(false);
  }
}

async function logout() {
  window.clearInterval(state.refreshTimer);
  state.refreshTimer = null;
  const token = state.token;
  if (token) {
    await api("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
  }
  state.token = "";
  state.principal = null;
  state.robots = [];
  state.agents = [];
  state.scenarios = [];
  state.audit = [];
  elements.app.hidden = true;
  elements.loginView.hidden = false;
  elements.adminName.textContent = "";
  elements.password.value = "";
  render();
}

async function createRobot(event) {
  event.preventDefault();
  const displayName = elements.robotDisplayName.value.trim();
  if (!displayName) {
    showToast("请填写机器人名称", "danger");
    return;
  }
  try {
    const scenarioId = await ensureDefaultScenario();
    const robot = await api("/api/admin/robots", {
      method: "POST",
      body: { displayName, scenarioId },
    });
    elements.robotDisplayName.value = "";
    showSecret({
      eyebrow: "机器人已创建",
      title: robot.displayName,
      message: "设备凭证仅显示一次。",
      labelOne: "Robot ID",
      valueOne: robot.id,
      labelTwo: "Credential",
      valueTwo: robot.credential,
    });
    await refreshAll();
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

async function robotAction(robotId, action) {
  try {
    await api(`/api/admin/robots/${encodeURIComponent(robotId)}/${action}`, {
      method: "POST",
      body: {},
    });
    showToast("机器人状态已更新", "success");
    await refreshAll();
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

async function renameRobot(robot) {
  const displayName = window.prompt(
    "请输入机器人新名称",
    robot.displayName || "",
  );
  if (displayName === null) {
    return;
  }
  try {
    await api(`/api/admin/robots/${encodeURIComponent(robot.id)}`, {
      method: "PATCH",
      body: { displayName: displayName.trim() },
    });
    showToast("机器人名称已更新", "success");
    await refreshAll();
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

async function deleteRobot(robot) {
  if (!window.confirm(`确认删除机器人“${robot.displayName}”吗？`)) {
    return;
  }
  try {
    await api(`/api/admin/robots/${encodeURIComponent(robot.id)}`, {
      method: "DELETE",
    });
    showToast("机器人已从目录删除", "success");
    await refreshAll();
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

async function rotateRobotCredential(robot) {
  if (!window.confirm(`轮换“${robot.displayName}”的设备凭证？`)) {
    return;
  }
  try {
    const payload = await api(
      `/api/admin/robots/${encodeURIComponent(robot.id)}/rotate-credential`,
      { method: "POST", body: {} },
    );
    showSecret({
      eyebrow: "凭证已轮换",
      title: robot.displayName,
      message: "旧凭证立即失效。新凭证仅显示一次。",
      labelOne: "Robot ID",
      valueOne: payload.robotId,
      labelTwo: "Credential",
      valueTwo: payload.credential,
    });
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

async function createAgent(event) {
  event.preventDefault();
  try {
    const scenarioId = await ensureDefaultScenario();
    await api("/api/admin/agents", {
      method: "POST",
      body: {
        username: elements.agentUsername.value.trim(),
        displayName: elements.agentDisplayName.value.trim(),
        password: elements.agentPassword.value,
        scenarioIds: [scenarioId],
      },
    });
    const username = elements.agentUsername.value.trim();
    elements.agentUsername.value = "";
    elements.agentDisplayName.value = "";
    elements.agentPassword.value = "";
    showToast(`客服 ${username} 已创建`, "success");
    await refreshAll();
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

async function agentAction(agent, action) {
  try {
    await api(
      `/api/admin/agents/${encodeURIComponent(agent.id)}/${action}`,
      { method: "POST", body: {} },
    );
    showToast("客服账号状态已更新", "success");
    await refreshAll();
  } catch (error) {
    showToast(errorText(error), "danger");
  }
}

function showSecret({
  eyebrow,
  title,
  message,
  labelOne,
  valueOne,
  labelTwo,
  valueTwo,
}) {
  state.secretText = `${labelOne}: ${valueOne}\n${labelTwo}: ${valueTwo}`;
  elements.secretEyebrow.textContent = eyebrow;
  elements.secretTitle.textContent = title;
  elements.secretMessage.textContent = message;
  elements.secretLabelOne.textContent = labelOne;
  elements.secretValueOne.textContent = valueOne;
  elements.secretLabelTwo.textContent = labelTwo;
  elements.secretValueTwo.textContent = valueTwo;
  elements.secretDialog.showModal();
}

async function copySecret() {
  try {
    await navigator.clipboard.writeText(state.secretText);
    showToast("已复制", "success");
  } catch (_error) {
    showToast("浏览器未允许复制，请手动选择文本", "warning");
  }
}

function switchView(view) {
  for (const button of $$(".admin-nav-item")) {
    button.classList.toggle("active", button.dataset.adminView === view);
  }
  const panels = {
    robots: $("#robotsPanel"),
    agents: $("#agentsPanel"),
    audit: $("#auditPanel"),
  };
  for (const [name, panel] of Object.entries(panels)) {
    panel.hidden = name !== view;
  }
}

async function loadBackendConfig() {
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
}

async function initialize() {
  elements.loginForm.addEventListener("submit", login);
  elements.logoutButton.addEventListener("click", () => void logout());
  elements.refreshButton.addEventListener("click", () => void refreshAll());
  elements.createRobotForm.addEventListener("submit", createRobot);
  elements.createAgentForm.addEventListener("submit", createAgent);
  elements.copySecretButton.addEventListener("click", () => void copySecret());
  for (const button of $$(".admin-nav-item")) {
    button.addEventListener("click", () => switchView(button.dataset.adminView));
  }

  try {
    await loadBackendConfig();
    elements.loginButton.disabled = false;
  } catch (error) {
    setLoginError(`无法加载控制端配置：${errorText(error)}`);
  }
}

void initialize();
