const VIEWER_MODULE_TIMEOUT_MS = 10000;

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
let operationSession = null;

function notice(id, message) {
  const target = element(id);
  target.textContent = message;
  target.hidden = !message;
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
    throw new Error(typeof data.error === "string" ? data.error : "요청을 처리하지 못했습니다. 다시 시도해 주세요.");
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

function disconnect() {
  connectionAttempt += 1;
  if (connection) {
    const previous = connection;
    connection = null;
    previous.disconnect();
  }
  element("desktop").replaceChildren();
  setConnectionStatus("연결 종료");
}

async function openSession(session) {
  disconnect();
  const attempt = connectionAttempt;
  activeSession = session;
  workspace.hidden = true;
  viewer.hidden = false;
  element("viewer-title").textContent = sessionName(session);
  element("reconnect-viewer").hidden = true;
  notice("viewer-notice", "");
  setConnectionStatus("연결 중…");
  try {
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
    remote.scaleViewport = true;
    remote.resizeSession = element("resize-desktop").checked;
    remote.background = "#0c1012";
    remote.addEventListener("connect", () => {
      if (connection !== remote) return;
      setConnectionStatus("연결됨", true);
      remote.focus();
    });
    remote.addEventListener("disconnect", async () => {
      if (connection !== remote) return;
      connection = null;
      setConnectionStatus("연결 종료");
      element("reconnect-viewer").hidden = false;
      notice("viewer-notice", "화면 연결이 종료되었습니다. 다시 연결할 수 있습니다.");
      try {
        await api("/api/sessions/");
      } catch (error) {
        notice("viewer-notice", error.message);
      }
    });
    remote.addEventListener("securityfailure", () => {
      if (connection === remote) notice("viewer-notice", "데스크톱 인증에 실패했습니다. 다시 연결해 주세요.");
    });
    remote.addEventListener("credentialsrequired", () => {
      if (connection !== remote) return;
      disconnect();
      element("reconnect-viewer").hidden = false;
      notice("viewer-notice", "데스크톱 인증 정보를 확인하지 못했습니다. 다시 연결해 주세요.");
    });
  } catch (error) {
    if (attempt !== connectionAttempt) return;
    setConnectionStatus("연결 실패");
    element("reconnect-viewer").hidden = false;
    notice("viewer-notice", error.message);
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
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});
refreshSessions();
