const VIEWER_MODULE_TIMEOUT_MS = 10000;
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];
const RECONNECT_MAX_ATTEMPTS = 10;
const RETRYABLE_CLIENT_STATUSES = new Set([408, 429]);
const RECONNECT_NOTICE = "화면 연결이 끊어졌습니다. 자동으로 다시 연결합니다.";
const AUTHENTICATION_FAILED = "데스크톱 인증에 실패했습니다. 다시 연결해 주세요.";
// Xvnc drops clipboard updates longer than its MaxCutText default.
const CLIPBOARD_MAX_BYTES = 262144;
const clipboardEncoder = new TextEncoder();
const LOGIN_EXPIRED = "로그인이 만료되었습니다. 다시 로그인해 주세요.";
// The gateway reads a whole request body before its view runs, so files go up in pieces of this size.
const UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024;
const DOWNLOADS_LABEL = "Downloads";
const UPLOAD_NETWORK_ERROR = "네트워크 연결이 끊어져 올리지 못했습니다. 다시 시도해 주세요.";
const UPLOAD_CANCELLED = Symbol("upload cancelled");
const UPLOAD_STATUS = {
  queued: "대기 중",
  starting: "준비 중…",
  uploading: "올리는 중",
  finishing: "저장 중…",
  conflict: "같은 이름이 있습니다",
  done: "완료",
  skipped: "건너뜀",
  cancelled: "취소함",
};
const UPLOAD_CANCELLABLE = new Set(["queued", "starting", "uploading"]);
const UPLOAD_ENDED = new Set(["done", "skipped", "cancelled", "failed"]);

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
// The folder the file list shows, and the one the latest list request asked for.
let filesShown = "";
let filesWanted = "";
let filesRequest = 0;
const uploads = [];
let uploadRunning = false;
let unloadGuarded = false;
let leavingForLogin = false;

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
  ["invalid_path", "쓸 수 없는 경로나 파일 이름입니다. 이름의 길이와 문자를 확인해 주세요."],
  ["not_found", "파일이나 폴더를 찾지 못했습니다. 다른 곳에서 옮기거나 지웠을 수 있으니 목록을 새로고침해 주세요."],
  ["not_a_directory", "폴더가 아닙니다. 목록을 새로고침해 주세요."],
  ["not_a_file", "일반 파일이 아닙니다. 같은 이름의 폴더나 특수 파일이 있는지 확인해 주세요."],
  ["file_exists", "같은 이름의 파일이 이미 있습니다."],
  ["too_large", "파일이 너무 큽니다. 한 파일은 4 GiB까지 올릴 수 있습니다."],
  ["no_space", "서버의 디스크 공간이 부족합니다. 필요 없는 파일을 지운 뒤 다시 시도해 주세요."],
  ["permission_denied", "서버에서 이 파일이나 폴더에 접근할 권한이 없습니다."],
  ["upload_unknown", "업로드가 만료되었거나 취소되었습니다. 다시 올려 주세요."],
  ["offset_mismatch", "업로드 순서가 어긋났습니다. 다시 올려 주세요."],
  ["busy", "진행 중인 업로드가 너무 많습니다. 다른 업로드가 끝난 뒤 다시 시도해 주세요."],
  ["downloads_missing", "서버에 ~/Downloads 폴더가 없습니다. 세션을 만들거나 시작하면 생깁니다. 서버에서 raven web doctor로 확인해 주세요."],
  ["staging_invalid", "~/Downloads/.raven-uploads 폴더가 업로드를 막고 있습니다. 세션이나 서버에서 이 폴더를 지운 뒤 다시 올려 주세요."],
  ["cross_device", "올릴 폴더가 ~/Downloads와 다른 파일 시스템에 있어 저장할 수 없습니다. 다른 폴더에 올려 주세요."],
]);

function errorMessage(data) {
  const known = ERROR_MESSAGES.get(data.code);
  if (known) return known;
  if (typeof data.error !== "string") return "요청을 처리하지 못했습니다. 다시 시도해 주세요.";
  if (data.code === "session_failed") return `세션 작업에 실패했습니다: ${data.error}`;
  return data.error;
}

function apiError(status, data) {
  const error = new Error(errorMessage(data));
  error.status = status;
  error.code = data.code;
  return error;
}

function goToLogin() {
  leavingForLogin = true;
  updateUnloadGuard();
  disconnect();
  window.location.assign("/login/");
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
    goToLogin();
    throw new Error(LOGIN_EXPIRED);
  }
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json") ? await response.json() : {};
  if (!response.ok) throw apiError(response.status, data);
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
  element("clipboard-send").disabled = !connected;
}

function setClipboardStatus(message) {
  element("clipboard-status").textContent = message;
}

// The panel holds text only for the connection it was used with; nothing is logged or stored.
function clearClipboard() {
  element("clipboard-outgoing").value = "";
  element("clipboard-incoming").value = "";
  element("clipboard-copy").disabled = true;
  setClipboardStatus("");
  showClipboard(false);
}

function showClipboard(open) {
  element("clipboard-panel").hidden = !open;
  element("clipboard-toggle").setAttribute("aria-expanded", String(open));
}

function sendClipboard() {
  const text = element("clipboard-outgoing").value;
  if (!connection) {
    setClipboardStatus("화면이 연결되어 있지 않아 보내지 못했습니다.");
    return;
  }
  if (!text) {
    setClipboardStatus("보낼 텍스트가 없습니다.");
    return;
  }
  const size = clipboardEncoder.encode(text).length;
  if (size > CLIPBOARD_MAX_BYTES) {
    setClipboardStatus(`텍스트가 너무 깁니다 (${Math.ceil(size / 1024)} KiB). ${CLIPBOARD_MAX_BYTES / 1024} KiB 이하로 나눠 보내 주세요.`);
    return;
  }
  connection.clipboardPasteFrom(text);
  setClipboardStatus("세션 클립보드에 넣었습니다. 세션의 앱에서 붙여넣으세요.");
}

async function readPcClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    element("clipboard-outgoing").value = text;
    setClipboardStatus(text ? "PC 클립보드의 텍스트를 가져왔습니다. 세션으로 보내기를 누르세요." : "PC 클립보드가 비어 있습니다.");
  } catch {
    element("clipboard-outgoing").focus();
    setClipboardStatus("브라우저가 클립보드 읽기를 허용하지 않았습니다. 'PC → 세션' 칸에 직접 붙여넣으세요(Ctrl+V).");
  }
}

async function copyToPc() {
  const field = element("clipboard-incoming");
  try {
    await navigator.clipboard.writeText(field.value);
    setClipboardStatus("PC 클립보드에 복사했습니다.");
  } catch {
    field.focus();
    field.select();
    setClipboardStatus("브라우저가 클립보드 쓰기를 허용하지 않았습니다. 선택된 텍스트를 직접 복사하세요(Ctrl+C).");
  }
}

function filesJoin(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

function filesLabel(path) {
  return path ? `${DOWNLOADS_LABEL}/${path}` : DOWNLOADS_LABEL;
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function formatTime(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderBreadcrumb() {
  const parts = filesShown ? filesShown.split("/") : [];
  const crumbs = [{ label: DOWNLOADS_LABEL, path: "" }];
  parts.forEach((part, index) => crumbs.push({ label: part, path: parts.slice(0, index + 1).join("/") }));
  const items = crumbs.map((crumb, index) => {
    const item = document.createElement("li");
    const last = index === crumbs.length - 1;
    const control = document.createElement(last ? "span" : "button");
    control.className = last ? "crumb crumb-current" : "crumb";
    control.textContent = crumb.label;
    if (last) control.setAttribute("aria-current", "location");
    else {
      control.type = "button";
      control.addEventListener("click", () => listFiles(crumb.path));
    }
    item.append(control);
    return item;
  });
  element("files-breadcrumb").replaceChildren(...items);
  element("files-target").textContent = filesLabel(filesShown);
}

function fileRow(dir, entry) {
  const type = entry.type === "dir" || entry.type === "file" ? entry.type : "other";
  const path = filesJoin(dir, entry.name);
  const row = document.createElement("li");
  row.className = `file-row file-${type}`;
  let name;
  if (type === "dir") {
    name = document.createElement("button");
    name.type = "button";
    name.addEventListener("click", () => listFiles(path));
  } else if (type === "file") {
    name = document.createElement("a");
    name.href = `/api/files/download/?path=${encodeURIComponent(path)}`;
    name.download = entry.name;
    name.title = "내려받기";
  } else {
    // Links and special files are never followed or read from the web.
    name = document.createElement("span");
    name.title = "링크나 특수 파일은 웹에서 열거나 내려받을 수 없습니다.";
  }
  name.className = "file-name";
  name.textContent = entry.name;
  const size = document.createElement("span");
  size.className = "file-size";
  size.textContent = { dir: "폴더", file: formatSize(entry.size), other: "열 수 없음" }[type];
  const meta = document.createElement("span");
  meta.className = "file-meta";
  meta.append(size);
  if (Number.isFinite(entry.mtime)) {
    const time = document.createElement("time");
    time.className = "file-time";
    time.dateTime = new Date(entry.mtime * 1000).toISOString();
    time.textContent = formatTime(entry.mtime);
    meta.append(time);
  }
  row.append(name, meta);
  return row;
}

function renderFiles(data) {
  const entries = Array.isArray(data.entries) ? data.entries : [];
  const fragment = document.createDocumentFragment();
  for (const entry of entries) fragment.append(fileRow(filesShown, entry));
  element("files-list").replaceChildren(fragment);
  element("files-status").textContent = entries.length ? "" : "빈 폴더입니다.";
  const notes = [];
  if (data.truncated) {
    notes.push(`항목이 너무 많아 ${entries.length}개만 보입니다. 이름순으로 고른 것이 아닐 수 있으니 전체는 세션의 파일 관리자에서 확인해 주세요.`);
  }
  if (data.skipped > 0) notes.push(`이름을 읽을 수 없는 항목 ${data.skipped}개는 목록에서 뺐습니다.`);
  notice("files-note", notes.join(" "));
}

async function listFiles(path = filesWanted) {
  const request = ++filesRequest;
  filesWanted = path;
  element("files-status").textContent = "불러오는 중…";
  notice("files-error", "");
  try {
    const data = await api(`/api/files/?path=${encodeURIComponent(path)}`);
    if (request !== filesRequest) return;
    filesShown = typeof data.path === "string" ? data.path : path;
    filesWanted = filesShown;
    renderFiles(data);
  } catch (error) {
    if (request !== filesRequest) return;
    // A folder that failed to open leaves the previous one in place; one that vanished is emptied.
    if (path === filesShown) {
      element("files-list").replaceChildren();
      notice("files-note", "");
    }
    filesWanted = filesShown;
    element("files-status").textContent = "";
    notice("files-error", error instanceof TypeError ? "목록을 불러오지 못했습니다. 네트워크 연결을 확인해 주세요." : error.message);
  }
  renderBreadcrumb();
}

function openFiles() {
  const dialog = element("files-dialog");
  if (!dialog.open) dialog.showModal();
  const current = uploads.find((upload) => !UPLOAD_ENDED.has(upload.state));
  if (current) revealUpload(current);
  listFiles(filesWanted);
}

function uploadPath(token) {
  return `/api/files/uploads/${encodeURIComponent(token)}/`;
}

function uploadsPending() {
  return uploads.some((upload) => UPLOAD_CANCELLABLE.has(upload.state) || upload.state === "finishing"
    || (upload.state === "conflict" && upload.token));
}

function warnBeforeUnload(event) {
  event.preventDefault();
  event.returnValue = "";
}

// Leaving the page drops every upload, so ask first, but only while one is still going.
function updateUnloadGuard() {
  const guard = !leavingForLogin && uploadsPending();
  if (guard === unloadGuarded) return;
  unloadGuarded = guard;
  if (guard) window.addEventListener("beforeunload", warnBeforeUnload);
  else window.removeEventListener("beforeunload", warnBeforeUnload);
}

function renderUploadList() {
  element("files-uploads").hidden = uploads.length === 0;
  element("files-clear-uploads").hidden = !uploads.some((upload) => UPLOAD_ENDED.has(upload.state));
  updateUnloadGuard();
}

function renderUpload(upload) {
  const { row, state } = upload;
  const size = upload.file.size;
  const sent = state === "done" ? size : Math.min(upload.sent, size);
  const progress = row.querySelector(".upload-progress");
  progress.max = size || 1;
  progress.value = state === "done" ? progress.max : sent;
  const percent = state === "done" ? 100 : size ? Math.floor((sent * 100) / size) : 0;
  row.dataset.state = state;
  row.querySelector(".upload-percent").textContent = `${percent}%`;
  let status = state === "failed" ? upload.message : UPLOAD_STATUS[state];
  if (state === "uploading") status += ` · ${formatSize(sent)} / ${formatSize(size)}`;
  row.querySelector(".upload-status").textContent = status;
  row.querySelector(".upload-cancel").hidden = !UPLOAD_CANCELLABLE.has(state);
  row.querySelector(".upload-overwrite").hidden = state !== "conflict";
  row.querySelector(".upload-skip").hidden = state !== "conflict";
  row.querySelector(".upload-retry").hidden = state !== "failed";
  renderUploadList();
}

// Keep the row that is running or waiting for an answer in sight; finished rows pile up above it.
function revealUpload(upload) {
  upload.row.scrollIntoView({ block: "nearest" });
}

function setUploadState(upload, state) {
  upload.state = state;
  renderUpload(upload);
  if (state === "uploading" || state === "conflict" || state === "failed") revealUpload(upload);
}

function addUpload(file, dir) {
  const row = element("upload-template").content.firstElementChild.cloneNode(true);
  const upload = { file, dir, row, state: "queued", sent: 0, confirmed: 0, token: null, xhr: null, overwrite: false, message: "" };
  row.querySelector(".upload-name").textContent = file.name;
  row.querySelector(".upload-destination").textContent = `${filesLabel(dir)}에 올립니다`;
  row.querySelector(".upload-cancel").addEventListener("click", () => cancelUpload(upload));
  row.querySelector(".upload-overwrite").addEventListener("click", () => resolveConflict(upload, true));
  row.querySelector(".upload-skip").addEventListener("click", () => resolveConflict(upload, false));
  row.querySelector(".upload-retry").addEventListener("click", () => retryUpload(upload));
  element("files-upload-list").append(row);
  uploads.push(upload);
  renderUpload(upload);
}

// The broker also drops an upload that stays idle, so a failed cancel only delays the cleanup.
function discardUpload(upload) {
  const { token } = upload;
  upload.token = null;
  if (token) api(uploadPath(token), "DELETE").catch(() => {});
}

function putChunk(upload, offset, body) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const url = new URL(`${uploadPath(upload.token)}?offset=${offset}`, window.location.href);
    upload.xhr = xhr;
    xhr.open("PUT", url.href);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-CSRFToken", csrfToken);
    xhr.upload.addEventListener("progress", (event) => {
      if (upload.xhr !== xhr) return;
      upload.sent = offset + Math.min(event.loaded, body.byteLength);
      renderUpload(upload);
    });
    xhr.addEventListener("load", () => {
      upload.xhr = null;
      if (xhr.status === 401 || (xhr.responseURL && new URL(xhr.responseURL).pathname !== url.pathname)) {
        goToLogin();
        reject(new Error(LOGIN_EXPIRED));
        return;
      }
      let data = {};
      if ((xhr.getResponseHeader("Content-Type") || "").includes("application/json")) {
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          data = {};
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(apiError(xhr.status, data));
    });
    xhr.addEventListener("error", () => {
      upload.xhr = null;
      reject(new TypeError("network error"));
    });
    xhr.addEventListener("abort", () => {
      upload.xhr = null;
      reject(UPLOAD_CANCELLED);
    });
    xhr.send(body);
  });
}

async function sendChunks(upload) {
  const { file } = upload;
  while (upload.sent < file.size) {
    const offset = upload.sent;
    const end = Math.min(offset + upload.chunkSize, file.size);
    let body;
    try {
      body = await file.slice(offset, end).arrayBuffer();
    } catch {
      throw new Error("PC의 파일을 읽지 못했습니다. 파일이 바뀌거나 지워졌는지 확인해 주세요.");
    }
    if (upload.state !== "uploading") throw UPLOAD_CANCELLED;
    const data = await putChunk(upload, offset, body);
    if (upload.state !== "uploading") throw UPLOAD_CANCELLED;
    if (data.received !== end) throw new Error("업로드 순서가 어긋났습니다. 다시 올려 주세요.");
    upload.sent = end;
    upload.confirmed = end;
    renderUpload(upload);
  }
}

async function finishUpload(upload) {
  setUploadState(upload, "finishing");
  await api(`${uploadPath(upload.token)}finish/`, "POST", { overwrite: upload.overwrite });
  upload.token = null;
  upload.sent = upload.file.size;
  setUploadState(upload, "done");
  if (element("files-dialog").open) listFiles();
}

function failUpload(upload, error) {
  if (error === UPLOAD_CANCELLED || !["starting", "uploading", "finishing"].includes(upload.state)) return;
  if (error.code === "file_exists") {
    // From finish the staged file stays on the server until the user answers.
    setUploadState(upload, "conflict");
    return;
  }
  discardUpload(upload);
  upload.sent = upload.confirmed;
  upload.message = error instanceof TypeError ? UPLOAD_NETWORK_ERROR : error.message;
  setUploadState(upload, "failed");
}

async function runUpload(upload) {
  upload.sent = 0;
  upload.confirmed = 0;
  setUploadState(upload, "starting");
  try {
    const started = await api("/api/files/uploads/", "POST", {
      path: upload.dir, name: upload.file.name, size: upload.file.size, overwrite: upload.overwrite,
    });
    upload.token = started.token;
    if (upload.state !== "starting") {
      discardUpload(upload);
      return;
    }
    upload.chunkSize = Number.isInteger(started.chunk_size) && started.chunk_size > 0 ? started.chunk_size : UPLOAD_CHUNK_BYTES;
    setUploadState(upload, "uploading");
    await sendChunks(upload);
    await finishUpload(upload);
  } catch (error) {
    failUpload(upload, error);
  }
}

// One file at a time, in the order they were chosen.
async function pumpUploads() {
  if (uploadRunning) return;
  uploadRunning = true;
  try {
    for (let next = uploads.find((upload) => upload.state === "queued"); next;
      next = uploads.find((upload) => upload.state === "queued")) {
      await runUpload(next);
    }
  } finally {
    uploadRunning = false;
  }
}

function cancelUpload(upload) {
  if (!UPLOAD_CANCELLABLE.has(upload.state)) return;
  upload.state = "cancelled";
  if (upload.xhr) upload.xhr.abort();
  discardUpload(upload);
  renderUpload(upload);
}

async function resolveConflict(upload, overwrite) {
  if (upload.state !== "conflict") return;
  if (!overwrite) {
    discardUpload(upload);
    setUploadState(upload, "skipped");
    return;
  }
  upload.overwrite = true;
  if (!upload.token) {
    setUploadState(upload, "queued");
    pumpUploads();
    return;
  }
  // Every byte is already on the server; only the final rename waited for the answer.
  try {
    await finishUpload(upload);
  } catch (error) {
    failUpload(upload, error);
  }
}

function retryUpload(upload) {
  if (upload.state !== "failed") return;
  upload.overwrite = false;
  upload.message = "";
  setUploadState(upload, "queued");
  pumpUploads();
}

function clearEndedUploads() {
  for (const upload of uploads.filter((item) => UPLOAD_ENDED.has(item.state))) {
    upload.row.remove();
    uploads.splice(uploads.indexOf(upload), 1);
  }
  renderUploadList();
  element("files-upload").focus();
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
  clearClipboard();
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
  remote.addEventListener("clipboard", (event) => {
    if (connection !== remote) return;
    element("clipboard-incoming").value = event.detail.text;
    element("clipboard-copy").disabled = !event.detail.text;
    if (!element("clipboard-panel").hidden) setClipboardStatus("세션에서 복사한 텍스트를 받았습니다.");
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
element("clipboard-toggle").addEventListener("click", () => {
  const open = element("clipboard-panel").hidden;
  showClipboard(open);
  if (open) element("clipboard-outgoing").focus();
  else if (connection) connection.focus();
});
element("files-open").addEventListener("click", openFiles);
element("viewer-files-open").addEventListener("click", openFiles);
element("files-refresh").addEventListener("click", () => listFiles());
element("files-upload").addEventListener("click", () => element("files-input").click());
element("files-input").addEventListener("change", (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  for (const file of files) addUpload(file, filesShown);
  pumpUploads();
});
element("files-clear-uploads").addEventListener("click", clearEndedUploads);
element("files-dialog").addEventListener("close", () => {
  if (!viewer.hidden && connection) connection.focus();
});
element("clipboard-send").addEventListener("click", sendClipboard);
element("clipboard-read").addEventListener("click", readPcClipboard);
element("clipboard-copy").addEventListener("click", copyToPc);
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
