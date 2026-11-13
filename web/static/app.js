const VIEWER_MODULE_TIMEOUT_MS = 10000;
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];
const RECONNECT_MAX_ATTEMPTS = 10;
const RETRYABLE_CLIENT_STATUSES = new Set([408, 429]);
const RECONNECT_NOTICE = "화면 연결이 끊어졌습니다. 자동으로 다시 연결합니다.";
const AUTHENTICATION_FAILED = "데스크톱 인증에 실패했습니다. 다시 연결해 주세요.";

async function loadViewerModule() {
  let timer;
  try {
    return await Promise.race([
      import("/static/novnc-1.7.0-raven1/core/rfb.js"),
      new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("화면 준비 시간이 초과되었습니다. 다시 연결해 주세요.")), VIEWER_MODULE_TIMEOUT_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

const element = (id) => document.getElementById(id);
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const workspace = element("workspace");
const viewer = element("viewer");
let activeSession = null;
let connection = null;
let connectionAttempt = 0;
let reconnectAttempts = 0;
let reconnectTimer = null;
let countdownTimer = null;
let operationSession = null;

function notice(id, message) {
  const target = element(id);
  target.textContent = message;
  target.hidden = !message;
}

const ERROR_MESSAGES = new Map([
  ["broker_unavailable", "호스트 중계에 연결할 수 없습니다. 서버에서 raven web doctor로 확인해 주세요."],
  ["csrf_cookie_missing", "보안 쿠키가 없습니다. 이 사이트의 쿠키를 허용하고 설정된 주소로 페이지를 다시 열어 주세요."],
  ["csrf_origin_mismatch", "다른 주소에서 보낸 요청으로 판단해 거부했습니다. 설정된 주소로 페이지를 다시 열어 주세요."],
  ["csrf_token_invalid", "페이지가 만료되었습니다. 새로고침한 뒤 다시 시도해 주세요."],
  ["invalid_host", "설정된 주소로 접속하지 않았습니다. 설정된 주소로 페이지를 다시 열어 주세요."],
]);

function errorMessage(data) {
  const known = ERROR_MESSAGES.get(data.code);
  if (known) return known;
  if (typeof data.error !== "string") return "요청을 처리하지 못했습니다. 다시 시도해 주세요.";
  if (data.code === "session_failed") return `세션 작업에 실패했습니다: ${data.error}`;
  return data.error;
}

async function api(path, method = "GET", body) {
  const headers = { Accept: "application/json" };
  const options = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401 || response.redirected) {
    disconnect();
    window.location.assign("/login/");
    throw new Error("로그인이 만료되었습니다. 다시 로그인해 주세요.");
  }
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json") ? await response.json() : {};
  if (!response.ok) {
    const error = new Error(errorMessage(data));
    error.status = response.status;
    throw error;
  }
  return data;
}

function sessionName(session) {
  return session.name || `세션 ${session.id}`;
}

function sessionPath(id, action = "") {
  return `/api/sessions/${encodeURIComponent(id)}/${action ? `${action}/` : ""}`;
}

function showDialog(kind, session = null) {
  operationSession = session;
  notice(`${kind}-error`, "");
  const form = element(`${kind}-form`);
  form.reset();
  for (const input of form.querySelectorAll("input")) input.setCustomValidity("");
  if (session) element(`${kind}-session-name`).textContent = sessionName(session);
  if (kind === "rename") element("rename-name").value = session.name || "";
  element(`${kind}-dialog`).showModal();
}

function renderSessions(sessions) {
  const list = element("session-list");
  const fragment = document.createDocumentFragment();
  for (const session of sessions) {
    const card = element("session-template").content.cloneNode(true);
    card.querySelector(".session-name").textContent = sessionName(session);
    const state = card.querySelector(".session-state");
    const stateNames = { running: "실행 중", exited: "중지됨", created: "시작 전", paused: "일시 중지", restarting: "재시작 중", dead: "오류" };
    state.textContent = stateNames[session.state] || session.state;
    state.classList.toggle("running", session.state === "running");
    card.querySelector(".session-details").textContent = `#${session.id} · ${session.wm === "xmonad" ? "XMonad" : "Openbox"} · ${session.geometry}`;
    const command = card.querySelector(".session-command");
    command.textContent = session.command;
    command.title = session.command;
    card.querySelector(".session-open").addEventListener("click", () => openSession(session));
    for (const button of card.querySelectorAll("[data-action]")) {
      button.addEventListener("click", () => showDialog(button.dataset.action, session));
    }
    fragment.append(card);
  }
  list.replaceChildren(fragment);
  element("session-count").textContent = String(sessions.length);
  element("empty-state").hidden = sessions.length !== 0;
}

async function refreshSessions() {
  const button = element("refresh-sessions");
  button.disabled = true;
  element("loading-status").textContent = "세션을 불러오는 중…";
  try {
    const data = await api("/api/sessions/");
    renderSessions(data.sessions);
    element("loading-status").textContent = "";
  } catch (error) {
    element("loading-status").textContent = "목록을 불러오지 못했습니다.";
    notice("workspace-notice", error.message);
  } finally {
    button.disabled = false;
  }
}

function setConnectionStatus(text, connected = false) {
  element("connection-status").textContent = text;
  element("connection-status").classList.toggle("connected", connected);
  element("send-ctrl-alt-del").disabled = !connected;
}

function clearReconnectTimers() {
  clearTimeout(reconnectTimer);
  clearInterval(countdownTimer);
  reconnectTimer = null;
  countdownTimer = null;
}

function disconnect() {
  connectionAttempt += 1;
  clearReconnectTimers();
  if (connection) {
    const previous = connection;
    connection = null;
    previous.disconnect();
  }
  element("desktop").replaceChildren();
  setConnectionStatus("연결 종료");
}

function endConnection(message, { status = "연결 종료", retry = true } = {}) {
  setConnectionStatus(status);
  element("reconnect-viewer").hidden = !retry;
  notice("viewer-notice", message);
}

function scheduleReconnect(message) {
  if (reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
    endConnection("화면에 다시 연결하지 못했습니다. 네트워크와 서버 상태를 확인한 뒤 다시 연결해 주세요.", { status: "연결 실패" });
    return;
  }
  const next = reconnectAttempts + 1;
  const delay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempts, RECONNECT_DELAYS_MS.length - 1)];
  const deadline = Date.now() + delay;
  const showCountdown = () => {
    const seconds = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
    setConnectionStatus(`다시 연결 중 (${next}/${RECONNECT_MAX_ATTEMPTS}, ${seconds}초 후)`);
  };
  showCountdown();
  countdownTimer = setInterval(showCountdown, 1000);
  reconnectTimer = setTimeout(reconnectNow, delay);
  element("reconnect-viewer").hidden = false;
  notice("viewer-notice", message);
}

// Every attempt rechecks the login and the session first: open would start a stopped desktop.
async function reconnectNow() {
  clearReconnectTimers();
  const attempt = connectionAttempt;
  const session = activeSession;
  reconnectAttempts += 1;
  setConnectionStatus(`다시 연결 중 (${reconnectAttempts}/${RECONNECT_MAX_ATTEMPTS})`);
  try {
    const { sessions } = await api("/api/sessions/");
    if (attempt !== connectionAttempt) return;
    const current = sessions.find((item) => item.id === session.id);
    if (!current || current.generation !== session.generation) {
      endConnection("세션이 삭제되었거나 다시 만들어졌습니다. 세션 목록에서 다시 선택해 주세요.", { retry: false });
      return;
    }
    if (current.state !== "running") {
      endConnection("세션이 중지되어 자동으로 다시 연결하지 않습니다. 다시 연결하면 세션을 시작합니다.");
      return;
    }
    activeSession = current;
    element("viewer-title").textContent = sessionName(current);
    await connectViewer(current, attempt);
  } catch (error) {
    if (attempt !== connectionAttempt) return;
    if (error.status >= 400 && error.status < 500 && !RETRYABLE_CLIENT_STATUSES.has(error.status)) {
      endConnection(error.message, { status: "연결 실패" });
      return;
    }
    // fetch rejects with a TypeError when the network or the gateway is unreachable.
    scheduleReconnect(error instanceof TypeError ? RECONNECT_NOTICE : error.message);
  }
}

async function connectViewer(session, attempt) {
  const { default: RFB } = await loadViewerModule();
  if (attempt !== connectionAttempt) return;
  const data = await api(sessionPath(session.id, "open"), "POST");
  if (attempt !== connectionAttempt) return;
  const url = new URL(data.websocket, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith("/ws/sessions/")) {
    throw new Error("화면 연결 주소가 올바르지 않습니다.");
  }
  url.protocol = "wss:";
  const remote = new RFB(element("desktop"), url.href, { credentials: { password: data.password } });
  data.password = "";
  connection = remote;
  let authenticationFailed = false;
  remote.scaleViewport = true;
  remote.resizeSession = element("resize-desktop").checked;
  remote.background = "#0c1012";
  remote.addEventListener("connect", () => {
    if (connection !== remote) return;
    reconnectAttempts = 0;
    element("reconnect-viewer").hidden = true;
    notice("viewer-notice", "");
    setConnectionStatus("연결됨", true);
    remote.focus();
  });
  remote.addEventListener("disconnect", () => {
    if (connection !== remote) return;
    connection = null;
    if (authenticationFailed) endConnection(AUTHENTICATION_FAILED);
    else scheduleReconnect(RECONNECT_NOTICE);
  });
  remote.addEventListener("securityfailure", () => {
    if (connection !== remote) return;
    authenticationFailed = true;
    notice("viewer-notice", AUTHENTICATION_FAILED);
  });
  remote.addEventListener("credentialsrequired", () => {
    if (connection !== remote) return;
    disconnect();
    endConnection("데스크톱 인증 정보를 확인하지 못했습니다. 다시 연결해 주세요.");
  });
}

async function openSession(session) {
  disconnect();
  const attempt = connectionAttempt;
  activeSession = session;
  reconnectAttempts = 0;
  workspace.hidden = true;
  viewer.hidden = false;
  element("viewer-title").textContent = sessionName(session);
  element("reconnect-viewer").hidden = true;
  notice("viewer-notice", "");
  setConnectionStatus("연결 중…");
  try {
    await connectViewer(session, attempt);
  } catch (error) {
    if (attempt !== connectionAttempt) return;
    endConnection(error.message, { status: "연결 실패" });
  }
}

async function closeViewer() {
  disconnect();
  activeSession = null;
  if (document.fullscreenElement) await document.exitFullscreen();
  viewer.hidden = true;
  workspace.hidden = false;
  element("refresh-sessions").focus();
  await refreshSessions();
}

function handleForm(kind, action) {
  const form = element(`${kind}-form`);
  const dialog = element(`${kind}-dialog`);
  const submit = async (event) => {
    event.preventDefault();
    if (dialog.dataset.busy) return;
    const fields = Object.fromEntries(new FormData(form));
    const session = operationSession;
    if (fields.command !== undefined) {
      fields.command = fields.command.trim();
      form.elements.command.setCustomValidity(fields.command ? "" : "명령을 입력하세요.");
    }
    if (kind === "delete") {
      element("delete-name").setCustomValidity(fields.name === sessionName(session) ? "" : "세션 이름을 정확히 입력하세요.");
    }
    if (!form.reportValidity()) return;
    const controls = [...form.elements];
    controls.forEach((control) => { control.disabled = true; });
    dialog.dataset.busy = "true";
    notice(`${kind}-error`, "");
    let createdSession = null;
    try {
      const result = await action(fields, session);
      notice("workspace-notice", "");
      await refreshSessions();
      if (kind === "create") createdSession = result.session;
      dialog.close();
    } catch (error) {
      notice(`${kind}-error`, error.message);
    } finally {
      controls.forEach((control) => { control.disabled = false; });
      delete dialog.dataset.busy;
    }
    if (createdSession) await openSession(createdSession);
  };
  if (kind === "delete") {
    form.addEventListener("submit", (event) => event.preventDefault());
    element("confirm-delete").addEventListener("click", submit);
  } else {
    form.addEventListener("submit", submit);
  }
}

element("new-session").addEventListener("click", () => showDialog("create"));
element("empty-new-session").addEventListener("click", () => showDialog("create"));
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("cancel", (event) => {
    if (dialog.dataset.busy) event.preventDefault();
  });
  dialog.querySelector("[data-close-dialog]").addEventListener("click", () => dialog.close());
  for (const input of dialog.querySelectorAll("input")) {
    input.addEventListener("input", () => input.setCustomValidity(""));
  }
}
handleForm("create", (fields) => api("/api/sessions/", "POST", fields));
handleForm("rename", (fields, session) => api(sessionPath(session.id, "rename"), "POST", { ...fields, generation: session.generation }));
handleForm("run", (fields, session) => api(sessionPath(session.id, "run"), "POST", { ...fields, generation: session.generation }));
handleForm("delete", (_fields, session) => api(sessionPath(session.id), "DELETE", { confirm: String(session.id), generation: session.generation }));

element("refresh-sessions").addEventListener("click", () => {
  notice("workspace-notice", "");
  refreshSessions();
});
element("close-viewer").addEventListener("click", closeViewer);
element("reconnect-viewer").addEventListener("click", () => {
  if (activeSession) openSession(activeSession);
});
element("resize-desktop").addEventListener("change", (event) => {
  if (connection) connection.resizeSession = event.target.checked;
});
element("send-ctrl-alt-del").addEventListener("click", () => {
  if (!connection) return;
  connection.sendCtrlAltDel();
  connection.focus();
});
element("fullscreen-viewer").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await viewer.requestFullscreen();
    if (connection) connection.focus();
  } catch {
    notice("viewer-notice", "이 브라우저에서는 전체 화면을 사용할 수 없습니다.");
  }
});
document.addEventListener("fullscreenchange", () => {
  element("fullscreen-viewer").textContent = document.fullscreenElement ? "전체 화면 종료" : "전체 화면";
});
element("logout-form").addEventListener("submit", disconnect);
window.addEventListener("pagehide", disconnect);
window.addEventListener("online", () => {
  if (reconnectTimer !== null) reconnectNow();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});
refreshSessions();
