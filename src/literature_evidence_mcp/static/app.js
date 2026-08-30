"use strict";

const state = {
  csrfToken: "",
  snapshots: [],
};

const elements = {
  buildButton: document.querySelector("#build-button"),
  buildStatus: document.querySelector("#build-status"),
  files: document.querySelector("#source-files"),
  query: document.querySelector("#query"),
  refreshButton: document.querySelector("#refresh-button"),
  searchButton: document.querySelector("#search-button"),
  searchResults: document.querySelector("#search-results"),
  searchStatus: document.querySelector("#search-status"),
  selectedFiles: document.querySelector("#selected-files"),
  serviceStatus: document.querySelector("#service-status"),
  snapshotEmpty: document.querySelector("#snapshot-empty"),
  snapshotRows: document.querySelector("#snapshot-rows"),
  snapshotSelect: document.querySelector("#snapshot-select"),
  uploadLimits: document.querySelector("#upload-limits"),
};

function setNotice(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : { error: "本机服务返回了无法识别的响应。" };
  if (!response.ok) {
    throw new Error(payload.error || "本机操作未完成。请刷新后重试。");
  }
  return payload;
}

function formatMiB(bytes) {
  return `${Math.round(bytes / 1024 / 1024)} MiB`;
}

async function loadStatus() {
  const payload = await api("/api/status");
  state.csrfToken = payload.csrf_token;
  const libraryMessage = payload.library.available
    ? `资料库已就绪；发现 ${payload.library.snapshot_candidates} 个候选快照。`
    : "资料库尚未建立；第一次成功构建时才会创建。";
  setNotice(elements.serviceStatus, `本机服务正常。${libraryMessage}`);
  elements.uploadLimits.textContent =
    `上限：${payload.upload_limits.max_files} 个文件；` +
    `单文件 ${formatMiB(payload.upload_limits.max_file_bytes)}；` +
    `合计 ${formatMiB(payload.upload_limits.max_total_bytes)}。`;
}

function appendTextCell(row, text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  row.append(cell);
  return cell;
}

function renderSnapshots() {
  elements.snapshotRows.replaceChildren();
  elements.snapshotSelect.replaceChildren();
  const verified = state.snapshots.filter((item) => item.verified);

  for (const snapshot of state.snapshots) {
    const row = document.createElement("tr");
    const idCell = appendTextCell(row, "");
    const id = document.createElement("code");
    id.textContent = snapshot.snapshot_id;
    idCell.append(id);
    appendTextCell(row, snapshot.verified ? "已核验" : `核验失败：${snapshot.error}`);
    const counts = snapshot.counts;
    appendTextCell(
      row,
      counts ? `${counts.documents} / ${counts.chunks}` : "—",
    );
    const actionCell = appendTextCell(row, "");
    const verifyButton = document.createElement("button");
    verifyButton.type = "button";
    verifyButton.className = "secondary";
    verifyButton.textContent = "重新核验";
    verifyButton.addEventListener("click", () => verifySnapshot(snapshot.snapshot_id));
    actionCell.append(verifyButton);
    elements.snapshotRows.append(row);
  }

  for (const snapshot of verified) {
    const option = document.createElement("option");
    option.value = snapshot.snapshot_id;
    option.textContent = snapshot.snapshot_id;
    elements.snapshotSelect.append(option);
  }
  elements.snapshotEmpty.hidden = state.snapshots.length !== 0;
  elements.searchButton.disabled = verified.length === 0 || !elements.query.value.trim();
}

async function loadSnapshots(preferredId = "") {
  try {
    const payload = await api("/api/snapshots");
    state.snapshots = payload.snapshots;
    renderSnapshots();
    if (preferredId && state.snapshots.some((item) => item.snapshot_id === preferredId)) {
      elements.snapshotSelect.value = preferredId;
    }
  } catch (error) {
    setNotice(elements.serviceStatus, error.message, true);
  }
}

async function verifySnapshot(snapshotId) {
  try {
    setNotice(elements.serviceStatus, "正在重新核验快照……");
    await api(`/api/snapshots/${encodeURIComponent(snapshotId)}/verify`);
    await loadSnapshots(snapshotId);
    setNotice(elements.serviceStatus, "快照核验通过。服务仍保持只读。 ");
  } catch (error) {
    setNotice(elements.serviceStatus, error.message, true);
    await loadSnapshots();
  }
}

function renderSelectedFiles() {
  elements.selectedFiles.replaceChildren();
  for (const file of elements.files.files) {
    const item = document.createElement("li");
    item.textContent = `${file.name} · ${file.size.toLocaleString()} 字节`;
    elements.selectedFiles.append(item);
  }
  elements.buildButton.disabled = elements.files.files.length === 0;
}

async function buildSnapshot() {
  const form = new FormData();
  for (const file of elements.files.files) {
    form.append("files", file, file.name);
  }
  elements.buildButton.disabled = true;
  setNotice(elements.buildStatus, "正在本机构建并核验全新快照……");
  try {
    const payload = await api("/api/build", {
      method: "POST",
      headers: {
        "X-Build-Intent": "create-new-snapshot",
        "X-CSRF-Token": state.csrfToken,
      },
      body: form,
    });
    elements.files.value = "";
    renderSelectedFiles();
    setNotice(elements.buildStatus, `已新增快照：${payload.snapshot_id}`);
    await loadStatus();
    await loadSnapshots(payload.snapshot_id);
  } catch (error) {
    setNotice(elements.buildStatus, error.message, true);
  } finally {
    elements.buildButton.disabled = elements.files.files.length === 0;
  }
}

function badge(text, unverified) {
  const element = document.createElement("span");
  element.className = unverified ? "badge unverified" : "badge";
  element.textContent = text;
  return element;
}

function renderResults(payload) {
  elements.searchResults.replaceChildren();
  if (!payload.found) {
    const hint = payload.input_hint ? ` ${payload.input_hint}` : "";
    setNotice(elements.searchStatus, `${payload.message}${hint}`);
    return;
  }
  setNotice(elements.searchStatus, `找到 ${payload.results.length} 条本地证据。`);
  for (const result of payload.results) {
    const article = document.createElement("article");
    const title = document.createElement("h3");
    title.textContent = `${result.title} · ${result.source_name}`;
    const anchor = document.createElement("p");
    anchor.className = "anchor";
    anchor.textContent = result.anchor_label;
    const excerpt = document.createElement("p");
    excerpt.className = "excerpt";
    excerpt.textContent = result.excerpt;
    const flags = document.createElement("div");
    flags.append(
      badge(result.fulltext_verified ? "全文已核验" : "全文未核验", !result.fulltext_verified),
      badge(result.formula_verified ? "公式已核验" : "公式未核验", !result.formula_verified),
    );
    article.append(title, anchor, excerpt, flags);
    elements.searchResults.append(article);
  }
}

async function search() {
  const query = elements.query.value.trim();
  const snapshotId = elements.snapshotSelect.value;
  if (!query || !snapshotId) {
    return;
  }
  elements.searchButton.disabled = true;
  setNotice(elements.searchStatus, "正在核验快照并执行本地 BM25 搜索……");
  try {
    const payload = await api("/api/search", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
      },
      body: JSON.stringify({
        snapshot_id: snapshotId,
        query,
        top_k: 5,
        excerpt_chars: 1000,
      }),
    });
    renderResults(payload);
  } catch (error) {
    elements.searchResults.replaceChildren();
    setNotice(elements.searchStatus, error.message, true);
  } finally {
    elements.searchButton.disabled = !elements.query.value.trim() || !elements.snapshotSelect.value;
  }
}

async function refreshAll() {
  try {
    await loadStatus();
    await loadSnapshots(elements.snapshotSelect.value);
  } catch (error) {
    setNotice(elements.serviceStatus, error.message, true);
  }
}

elements.files.addEventListener("change", renderSelectedFiles);
elements.buildButton.addEventListener("click", buildSnapshot);
elements.refreshButton.addEventListener("click", refreshAll);
elements.searchButton.addEventListener("click", search);
elements.query.addEventListener("input", () => {
  elements.searchButton.disabled = !elements.query.value.trim() || !elements.snapshotSelect.value;
});

refreshAll();
