"use strict";

const state = {
  csrfToken: "",
  libraries: [],
  libraryId: "",
  libraryEpoch: 0,
  libraryRequestId: 0,
  snapshotRequestId: 0,
  searchRequestId: 0,
  snapshots: [],
  files: [],
  fileStates: [],
  building: false,
  completed: false,
  searching: false,
  enhancedAvailable: false,
  enhancedSimulated: null,
  mcpGuide: null,
  mcpSelfCheckRequestId: 0,
  mcpSelfCheckController: null,
  mcpSelfChecking: false,
};

const MCP_GUIDE_UNAVAILABLE_MESSAGE = "本地 MCP 配置模板当前不可用。";
const MCP_INVALID_REPORT_MESSAGE =
  "本地离线自检返回的数据格式无效；未展示结果，也未写入任何客户端配置。";

const elements = {
  baseSnapshotSelect: document.querySelector("#base-snapshot-select"),
  buildBlank: document.querySelector("#build-blank"),
  buildButton: document.querySelector("#build-button"),
  buildInherit: document.querySelector("#build-inherit"),
  buildStatus: document.querySelector("#build-status"),
  buildSummary: document.querySelector("#build-summary"),
  createLibraryButton: document.querySelector("#create-library-button"),
  dropZone: document.querySelector("#drop-zone"),
  enhancedAvailability: document.querySelector("#enhanced-availability"),
  enhancedModeOption: document.querySelector("#enhanced-mode-option"),
  files: document.querySelector("#source-files"),
  libraryDescription: document.querySelector("#library-description"),
  libraryDetails: document.querySelector("#library-details"),
  libraryName: document.querySelector("#library-name"),
  librarySelect: document.querySelector("#library-select"),
  libraryStatus: document.querySelector("#library-status"),
  mcpCli: document.querySelector("#mcp-cli"),
  mcpCopyStatus: document.querySelector("#mcp-copy-status"),
  mcpSelfCheckButton: document.querySelector("#mcp-self-check-button"),
  mcpSelfCheckReport: document.querySelector("#mcp-self-check-report"),
  mcpSelfCheckStatus: document.querySelector("#mcp-self-check-status"),
  mcpToml: document.querySelector("#mcp-toml"),
  copyMcpCliButton: document.querySelector("#copy-mcp-cli-button"),
  copyMcpTomlButton: document.querySelector("#copy-mcp-toml-button"),
  query: document.querySelector("#query"),
  refreshButton: document.querySelector("#refresh-button"),
  searchButton: document.querySelector("#search-button"),
  searchAudit: document.querySelector("#search-audit"),
  searchMode: document.querySelector("#search-mode"),
  searchResults: document.querySelector("#search-results"),
  searchStatus: document.querySelector("#search-status"),
  selectedFiles: document.querySelector("#selected-files"),
  serviceStatus: document.querySelector("#service-status"),
  snapshotEmpty: document.querySelector("#snapshot-empty"),
  snapshotRows: document.querySelector("#snapshot-rows"),
  snapshotSelect: document.querySelector("#snapshot-select"),
  switchLibraryButton: document.querySelector("#switch-library-button"),
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
    const error = new Error(payload.error || "本机操作未完成。请刷新后重试。");
    error.payload = payload;
    throw error;
  }
  return payload;
}

function actionHeaders(intent, json = false) {
  const headers = {
    "X-Action-Intent": intent,
    "X-CSRF-Token": state.csrfToken,
  };
  if (json) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

function formatBytes(bytes) {
  return `${Number(bytes).toLocaleString()} 字节`;
}

function formatMiB(bytes) {
  return `${Math.round(bytes / 1024 / 1024)} MiB`;
}

function appendText(parent, tagName, text, className = "") {
  const child = document.createElement(tagName);
  child.textContent = text;
  if (className) {
    child.className = className;
  }
  parent.append(child);
  return child;
}

function appendDefinition(list, term, value) {
  appendText(list, "dt", term);
  appendText(list, "dd", value || "—");
}

function enhancedRunLabel(simulated) {
  if (simulated === true) {
    return "离线模拟增强搜索";
  }
  if (simulated === false) {
    return "联网增强搜索";
  }
  return "增强状态未知";
}

function validMcpGuide(guide) {
  if (!guide || typeof guide !== "object" || Array.isArray(guide)) {
    return false;
  }
  if (guide.state === "unavailable") {
    return typeof guide.reason === "string" && guide.reason.trim().length > 0;
  }
  return (
    guide.state === "copy_ready_not_configured" &&
    guide.server_name === "literature-evidence" &&
    guide.tool_count === 8 &&
    guide.api_key_required === false &&
    typeof guide.cli === "string" &&
    guide.cli.trim().length > 0 &&
    typeof guide.toml === "string" &&
    guide.toml.trim().length > 0
  );
}

function renderMcpGuide(guide) {
  state.mcpGuide = validMcpGuide(guide) &&
    guide.state === "copy_ready_not_configured"
    ? guide
    : null;
  elements.mcpCli.value = state.mcpGuide ? state.mcpGuide.cli : "";
  elements.mcpToml.value = state.mcpGuide ? state.mcpGuide.toml : "";
  const unavailable = !state.mcpGuide;
  elements.copyMcpCliButton.disabled = unavailable;
  elements.copyMcpTomlButton.disabled = unavailable;
  elements.mcpSelfCheckButton.disabled = unavailable || state.mcpSelfChecking;
  if (unavailable) {
    elements.mcpCli.value = "";
    elements.mcpToml.value = "";
    elements.mcpSelfCheckReport.replaceChildren();
    elements.mcpSelfCheckReport.hidden = true;
    setNotice(elements.mcpSelfCheckStatus, "");
    setNotice(
      elements.mcpCopyStatus,
      validMcpGuide(guide) ? guide.reason : MCP_GUIDE_UNAVAILABLE_MESSAGE,
      true,
    );
  } else {
    setNotice(elements.mcpCopyStatus, "");
  }
}

async function loadStatus() {
  const payload = await api("/api/status");
  state.csrfToken = payload.csrf_token;
  const reportedSimulated = payload.enhanced && payload.enhanced.simulated;
  state.enhancedSimulated = typeof reportedSimulated === "boolean"
    ? reportedSimulated
    : null;
  state.enhancedAvailable =
    payload.enhanced_available === true && state.enhancedSimulated !== null;
  renderMcpGuide(payload.mcp_guide);
  elements.enhancedModeOption.disabled = !state.enhancedAvailable;
  elements.enhancedModeOption.textContent = state.enhancedAvailable
    ? (state.enhancedSimulated
      ? "增强搜索（离线模拟）"
      : "增强搜索（显式联网外发）")
    : "增强搜索（当前不可用）";
  if (!state.enhancedAvailable && elements.searchMode.value === "enhanced") {
    elements.searchMode.value = "bm25";
    invalidateSearch();
  }
  const roles = payload.enhanced && Array.isArray(payload.enhanced.roles)
    ? payload.enhanced.roles
    : [];
  elements.enhancedAvailability.textContent = state.enhancedAvailable
    ? `${state.enhancedSimulated ? "离线模拟" : "联网增强"} transport 已配置：${roles.map((item) => `${item.role}=${item.provider}/${item.model_id}`).join("；")}。`
    : "增强搜索尚未配置或当前不可用；请使用默认 BM25。";
  setNotice(
    elements.serviceStatus,
    `本机服务正常；已登记 ${payload.library_count} 个资料库；默认离线 BM25。`,
  );
  elements.uploadLimits.textContent =
    `上限：${payload.upload_limits.max_files} 个文件；` +
    `单文件 ${formatMiB(payload.upload_limits.max_file_bytes)}；` +
    `合计 ${formatMiB(payload.upload_limits.max_total_bytes)}。`;
}

function selectedLibraryCandidate() {
  return state.libraries.find(
    (item) => item.library_id === elements.librarySelect.value,
  );
}

function resetLibraryContext() {
  state.libraryEpoch += 1;
  state.snapshotRequestId += 1;
  state.searchRequestId += 1;
  state.searching = false;
  state.snapshots = [];
  state.files = [];
  state.fileStates = [];
  state.completed = false;
  elements.files.value = "";
  elements.buildBlank.checked = true;
  elements.buildSummary.replaceChildren();
  elements.buildSummary.hidden = true;
  elements.searchResults.replaceChildren();
  setNotice(elements.buildStatus, "");
  setNotice(elements.searchStatus, "");
  renderSelectedFiles();
  renderSnapshots();
}

function libraryContextIsCurrent(libraryId, epoch) {
  return state.libraryId === libraryId && state.libraryEpoch === epoch;
}

function searchRequestIsCurrent(requestId, libraryId, epoch) {
  return (
    requestId === state.searchRequestId &&
    libraryContextIsCurrent(libraryId, epoch)
  );
}

function renderLibraryDetails() {
  elements.libraryDetails.replaceChildren();
  const library = selectedLibraryCandidate();
  if (!library) {
    appendDefinition(elements.libraryDetails, "状态", "尚未创建资料库");
    elements.switchLibraryButton.disabled = true;
    return;
  }
  appendDefinition(elements.libraryDetails, "名称", library.name);
  appendDefinition(elements.libraryDetails, "描述", library.description || "未填写");
  appendDefinition(elements.libraryDetails, "稳定 library_id", library.library_id);
  appendDefinition(
    elements.libraryDetails,
    "当前快照",
    library.current_snapshot_id,
  );
  appendDefinition(
    elements.libraryDetails,
    "上次成功快照",
    library.last_successful_snapshot_id,
  );
  elements.switchLibraryButton.disabled =
    library.library_id === state.libraryId || state.building;
}

function renderLibraries(candidateId = "") {
  const previousLibraryId = state.libraryId;
  elements.librarySelect.replaceChildren();
  for (const library of state.libraries) {
    const option = document.createElement("option");
    option.value = library.library_id;
    option.textContent = `${library.name}${library.selected ? "（当前）" : ""}`;
    elements.librarySelect.append(option);
  }
  const active = state.libraries.find((item) => item.selected);
  state.libraryId = active ? active.library_id : "";
  const candidate = state.libraries.some((item) => item.library_id === candidateId)
    ? candidateId
    : state.libraryId;
  if (candidate) {
    elements.librarySelect.value = candidate;
  }
  if (previousLibraryId !== state.libraryId) {
    resetLibraryContext();
  }
  renderLibraryDetails();
  updateBuildControls();
}

async function loadLibraries(candidateId = "") {
  const requestId = ++state.libraryRequestId;
  const payload = await api("/api/libraries");
  if (requestId !== state.libraryRequestId) {
    return false;
  }
  state.libraries = payload.libraries;
  renderLibraries(candidateId);
  return true;
}

async function createLibrary() {
  const name = elements.libraryName.value.trim();
  if (!name) {
    setNotice(elements.libraryStatus, "请先填写资料库名称。", true);
    return;
  }
  elements.createLibraryButton.disabled = true;
  setNotice(elements.libraryStatus, "正在本机创建资料库……");
  try {
    const payload = await api("/api/libraries", {
      method: "POST",
      headers: actionHeaders("create-library", true),
      body: JSON.stringify({
        name,
        description: elements.libraryDescription.value.trim(),
      }),
    });
    elements.libraryName.value = "";
    elements.libraryDescription.value = "";
    if (!(await loadLibraries())) {
      return;
    }
    if (!(await loadSnapshots())) {
      return;
    }
    setNotice(
      elements.libraryStatus,
      `已创建“${payload.library.name}”；稳定 ID：${payload.library.library_id}。`,
    );
  } catch (error) {
    setNotice(elements.libraryStatus, error.message, true);
  } finally {
    elements.createLibraryButton.disabled = false;
  }
}

async function switchLibrary() {
  const libraryId = elements.librarySelect.value;
  if (!libraryId || state.building) {
    return;
  }
  elements.switchLibraryButton.disabled = true;
  setNotice(elements.libraryStatus, "正在明确切换资料库……");
  try {
    await api(`/api/libraries/${encodeURIComponent(libraryId)}/select`, {
      method: "POST",
      headers: actionHeaders("select-library"),
    });
    if (!(await loadLibraries(libraryId))) {
      return;
    }
    if (!(await loadSnapshots())) {
      return;
    }
    const active = state.libraries.find((item) => item.library_id === state.libraryId);
    setNotice(elements.libraryStatus, `已切换到“${active.name}”。`);
  } catch (error) {
    setNotice(elements.libraryStatus, error.message, true);
    renderLibraryDetails();
  }
}

function badge(text, unverified = false) {
  const element = document.createElement("span");
  element.className = unverified ? "badge unverified" : "badge";
  element.textContent = text;
  return element;
}

function memberNames(items) {
  return items.map((item) => item.source_name).join("、") || "无";
}

function renderSnapshotDifference(cell, snapshot) {
  if (!snapshot.difference || !snapshot.storage) {
    cell.textContent = snapshot.difference_error || "—";
    return;
  }
  const counts = snapshot.difference.counts;
  appendText(
    cell,
    "p",
    `差异：新增 ${counts.added}；继承 ${counts.inherited}；替换 ${counts.replaced}；移除 ${counts.removed}`,
  );
  appendText(cell, "p", `新增：${memberNames(snapshot.difference.added)}`, "hint");
  const storage = snapshot.storage;
  appendText(
    cell,
    "p",
    `对象引用 ${storage.object_references}；新增 ${storage.new_objects}；复用 ${storage.reused_objects}`,
  );
  appendText(
    cell,
    "p",
    `新增对象 ${formatBytes(storage.new_object_bytes)}；复用对象 ${formatBytes(storage.reused_object_bytes)}`,
    "hint",
  );
}

function renderSnapshots() {
  const searchValue = elements.snapshotSelect.value;
  const baseValue = elements.baseSnapshotSelect.value;
  elements.snapshotRows.replaceChildren();
  elements.snapshotSelect.replaceChildren();
  elements.baseSnapshotSelect.replaceChildren();
  const verified = state.snapshots.filter((item) => item.verified);

  for (const snapshot of state.snapshots) {
    const row = document.createElement("tr");
    const idCell = document.createElement("td");
    appendText(idCell, "code", snapshot.snapshot_id);
    const flags = document.createElement("div");
    if (snapshot.current) {
      flags.append(badge("current"));
    }
    if (snapshot.last_successful) {
      flags.append(badge("last-successful"));
    }
    if (snapshot.base_snapshot_id) {
      flags.append(badge("继承构建"));
    }
    idCell.append(flags);
    row.append(idCell);

    const verificationCell = document.createElement("td");
    verificationCell.textContent = snapshot.verified
      ? "已实时核验"
      : `核验失败：${snapshot.error}`;
    row.append(verificationCell);

    const membersCell = document.createElement("td");
    if (snapshot.members.length === 0) {
      membersCell.textContent = "—";
    } else {
      const list = document.createElement("ul");
      list.className = "compact-list";
      for (const member of snapshot.members) {
        appendText(list, "li", `${member.source_name} · ${member.chunk_count} 片段`);
      }
      membersCell.append(list);
    }
    row.append(membersCell);

    const differenceCell = document.createElement("td");
    renderSnapshotDifference(differenceCell, snapshot);
    row.append(differenceCell);

    const actionCell = document.createElement("td");
    const verifyButton = document.createElement("button");
    verifyButton.type = "button";
    verifyButton.className = "secondary small-button";
    verifyButton.textContent = "重新核验";
    verifyButton.disabled = elements.librarySelect.value !== state.libraryId;
    verifyButton.addEventListener("click", () => verifySnapshot(snapshot.snapshot_id));
    actionCell.append(verifyButton);
    const activateButton = document.createElement("button");
    activateButton.type = "button";
    activateButton.className = "secondary small-button";
    activateButton.textContent = snapshot.current ? "已激活" : "明确激活";
    activateButton.disabled =
      snapshot.current ||
      !snapshot.verified ||
      elements.librarySelect.value !== state.libraryId;
    activateButton.addEventListener("click", () => activateSnapshot(snapshot.snapshot_id));
    actionCell.append(activateButton);
    row.append(actionCell);
    elements.snapshotRows.append(row);
  }

  for (const snapshot of verified) {
    for (const select of [elements.snapshotSelect, elements.baseSnapshotSelect]) {
      const option = document.createElement("option");
      option.value = snapshot.snapshot_id;
      option.textContent = snapshot.snapshot_id;
      select.append(option);
    }
  }
  if (verified.some((item) => item.snapshot_id === searchValue)) {
    elements.snapshotSelect.value = searchValue;
  }
  if (verified.some((item) => item.snapshot_id === baseValue)) {
    elements.baseSnapshotSelect.value = baseValue;
  }
  elements.snapshotEmpty.hidden = state.snapshots.length !== 0;
  updateBuildControls();
  updateSearchButton();
}

async function loadSnapshots(preferredId = "") {
  const requestId = ++state.snapshotRequestId;
  const previousSnapshotId = elements.snapshotSelect.value;
  if (!state.libraryId) {
    state.snapshots = [];
    renderSnapshots();
    return true;
  }
  const requestedLibraryId = state.libraryId;
  const requestedEpoch = state.libraryEpoch;
  try {
    const payload = await api(
      `/api/libraries/${encodeURIComponent(requestedLibraryId)}/snapshots`,
    );
    if (
      requestId !== state.snapshotRequestId ||
      !libraryContextIsCurrent(requestedLibraryId, requestedEpoch)
    ) {
      return false;
    }
    state.snapshots = payload.snapshots;
    renderSnapshots();
    if (preferredId && state.snapshots.some((item) => item.snapshot_id === preferredId)) {
      elements.snapshotSelect.value = preferredId;
      elements.baseSnapshotSelect.value = preferredId;
    }
    if (elements.snapshotSelect.value !== previousSnapshotId) {
      invalidateSearch();
    }
    return true;
  } catch (error) {
    if (
      requestId !== state.snapshotRequestId ||
      !libraryContextIsCurrent(requestedLibraryId, requestedEpoch)
    ) {
      return false;
    }
    state.snapshots = [];
    renderSnapshots();
    setNotice(elements.serviceStatus, error.message, true);
    return false;
  }
}

async function verifySnapshot(snapshotId) {
  const requestedLibraryId = state.libraryId;
  const requestedEpoch = state.libraryEpoch;
  try {
    setNotice(elements.serviceStatus, "正在对明确资料库和快照做实时核验……");
    await api(
      `/api/libraries/${encodeURIComponent(requestedLibraryId)}/snapshots/${encodeURIComponent(snapshotId)}/verify`,
    );
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    if (!(await loadSnapshots(snapshotId))) {
      return;
    }
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    setNotice(elements.serviceStatus, "所选快照实时核验通过。这里只执行了本机只读检查。");
  } catch (error) {
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    setNotice(elements.serviceStatus, error.message, true);
    await loadSnapshots();
  }
}

async function activateSnapshot(snapshotId) {
  const requestedLibraryId = state.libraryId;
  const requestedEpoch = state.libraryEpoch;
  setNotice(elements.serviceStatus, "正在明确激活已核验快照……");
  try {
    await api(
      `/api/libraries/${encodeURIComponent(requestedLibraryId)}/snapshots/${encodeURIComponent(snapshotId)}/activate`,
      {
        method: "POST",
        headers: actionHeaders("activate-snapshot"),
      },
    );
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    if (!(await loadLibraries(requestedLibraryId))) {
      return;
    }
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    if (!(await loadSnapshots(snapshotId))) {
      return;
    }
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    setNotice(elements.serviceStatus, "已激活所选快照；上次成功快照指针未被暗中改写。");
  } catch (error) {
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    setNotice(elements.serviceStatus, error.message, true);
  }
}

function acceptedFile(file) {
  const name = file.name.toLowerCase();
  return name.endsWith(".md") || name.endsWith(".markdown") || name.endsWith(".pdf");
}

function chooseFiles(fileList) {
  const files = Array.from(fileList);
  elements.files.value = "";
  const unsupported = files.find((file) => !acceptedFile(file));
  if (unsupported) {
    setNotice(
      elements.buildStatus,
      `不接受 ${unsupported.name}；这里只接受 .md、.markdown、.pdf。`,
      true,
    );
    return;
  }
  state.files = files;
  state.fileStates = files.map(() => ({ stage: "待提交", error: "" }));
  state.completed = false;
  elements.buildSummary.hidden = true;
  renderSelectedFiles();
}

function renderSelectedFiles() {
  elements.selectedFiles.replaceChildren();
  state.files.forEach((file, index) => {
    const item = document.createElement("li");
    const status = state.fileStates[index] || { stage: "待提交", error: "" };
    appendText(item, "strong", file.name);
    appendText(item, "span", ` · ${formatBytes(file.size)} · ${status.stage}`);
    if (status.error) {
      appendText(item, "span", `：${status.error}`, "file-error");
    }
    elements.selectedFiles.append(item);
  });
  updateBuildControls();
}

function updateBuildControls() {
  const hasVerified = state.snapshots.some((item) => item.verified);
  const activeLibraryIsShown = elements.librarySelect.value === state.libraryId;
  elements.buildInherit.disabled = !hasVerified || !state.libraryId || state.building;
  if (!hasVerified && elements.buildInherit.checked) {
    elements.buildBlank.checked = true;
  }
  elements.baseSnapshotSelect.disabled =
    !elements.buildInherit.checked || !hasVerified || state.building;
  elements.buildButton.disabled =
    !state.libraryId ||
    !activeLibraryIsShown ||
    state.files.length === 0 ||
    state.building ||
    state.completed ||
    (elements.buildInherit.checked && !elements.baseSnapshotSelect.value);
}

function renderBuildSummary(payload) {
  elements.buildSummary.replaceChildren();
  elements.buildSummary.hidden = false;
  appendText(elements.buildSummary, "h3", `成功快照 ${payload.snapshot_id}`);
  const counts = payload.difference.counts;
  appendText(
    elements.buildSummary,
    "p",
    `相对所选基础：新增 ${counts.added}；继承 ${counts.inherited}；替换 ${counts.replaced}；移除 ${counts.removed}。`,
  );
  appendText(
    elements.buildSummary,
    "p",
    `完整成员：${memberNames(payload.members)}。`,
  );
  const storage = payload.storage;
  appendText(
    elements.buildSummary,
    "p",
    `对象引用 ${storage.object_references}；新增对象 ${storage.new_objects}；复用对象 ${storage.reused_objects}；新增对象字节 ${formatBytes(storage.new_object_bytes)}；复用对象字节 ${formatBytes(storage.reused_object_bytes)}。`,
  );
  appendText(
    elements.buildSummary,
    "p",
    "以上只统计 source/parsed/chunks 对象 payload；每个快照独立的 BM25 固定开销不计入节省量。",
    "hint",
  );
  if (payload.cleanup_warning) {
    appendText(elements.buildSummary, "p", payload.cleanup_warning, "file-error");
  }
}

async function buildSnapshot() {
  if (!state.libraryId || state.files.length === 0) {
    return;
  }
  const form = new FormData();
  const requestedLibraryId = state.libraryId;
  const requestedEpoch = state.libraryEpoch;
  const buildMode = elements.buildInherit.checked ? "inherit" : "blank";
  form.append("build_mode", buildMode);
  if (buildMode === "inherit") {
    form.append("base_snapshot_id", elements.baseSnapshotSelect.value);
  }
  for (const file of state.files) {
    form.append("files", file, file.name);
  }
  state.building = true;
  renderLibraryDetails();
  state.fileStates = state.files.map(() => ({
    stage: "本机接收 / 构建中",
    error: "",
  }));
  renderSelectedFiles();
  setNotice(elements.buildStatus, "本机正在接收、解析、构建并完整核验；尚未发布。");
  try {
    const payload = await api(
      `/api/libraries/${encodeURIComponent(requestedLibraryId)}/build`,
      {
        method: "POST",
        headers: actionHeaders("build-snapshot"),
        body: form,
      },
    );
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    state.fileStates = payload.files.map((item) => ({
      stage: item.stage,
      error: item.error || "",
    }));
    state.completed = true;
    elements.files.value = "";
    renderSelectedFiles();
    renderBuildSummary(payload);
    const cleanupWarning = payload.cleanup_warning
      ? ` ${payload.cleanup_warning}`
      : "";
    setNotice(
      elements.buildStatus,
      `整批已发布：${payload.snapshot_id}。${cleanupWarning}`,
    );
    if (!(await loadLibraries(requestedLibraryId))) {
      return;
    }
    if (!(await loadSnapshots(payload.snapshot_id))) {
      return;
    }
  } catch (error) {
    if (!libraryContextIsCurrent(requestedLibraryId, requestedEpoch)) {
      return;
    }
    const returned = error.payload && Array.isArray(error.payload.files)
      ? error.payload.files
      : state.files.map(() => ({ stage: "失败、未发布", error: error.message }));
    state.fileStates = state.files.map((_file, index) => {
      const item = returned[index];
      return {
        stage: item ? item.stage : "失败、未发布",
        error: item ? item.error : error.message,
      };
    });
    renderSelectedFiles();
    setNotice(elements.buildStatus, `${error.message} 没有产生残缺成功快照。`, true);
  } finally {
    state.building = false;
    renderLibraryDetails();
    updateBuildControls();
  }
}

function renderResults(payload) {
  elements.searchResults.replaceChildren();
  renderSearchAudit(payload.audit);
  if (!payload.found) {
    const hint = payload.input_hint ? ` ${payload.input_hint}` : "";
    const prefix = payload.retrieval_mode === "enhanced"
      ? `${enhancedRunLabel(payload.audit && payload.audit.simulated)}：`
      : "";
    setNotice(elements.searchStatus, `${prefix}${payload.message}${hint}`);
    return;
  }
  const modeLabel = payload.retrieval_mode === "enhanced"
    ? `${enhancedRunLabel(payload.audit && payload.audit.simulated)}证据`
    : "本地 BM25 证据";
  setNotice(elements.searchStatus, `找到 ${payload.results.length} 条${modeLabel}。`);
  for (const result of payload.results) {
    const article = document.createElement("article");
    appendText(article, "h3", `${result.title} · ${result.source_name}`);
    appendText(article, "p", result.anchor_label, "anchor");
    appendText(article, "p", result.excerpt, "excerpt");
    const flags = document.createElement("div");
    flags.append(
      badge(result.fulltext_verified ? "全文已核验" : "全文未核验", !result.fulltext_verified),
      badge(result.formula_verified ? "公式已核验" : "公式未核验", !result.formula_verified),
    );
    article.append(flags);
    elements.searchResults.append(article);
  }
}

function renderSearchAudit(audit) {
  elements.searchAudit.replaceChildren();
  elements.searchAudit.hidden = !audit;
  if (!audit) {
    return;
  }
  appendText(
    elements.searchAudit,
    "h3",
    `${enhancedRunLabel(audit.simulated)}调用审计：实际 ${audit.call_count} 次`,
  );
  const list = document.createElement("ul");
  for (const call of audit.calls) {
    const details = [`${call.role} · ${call.provider}/${call.model_id}`, call.sent];
    if (Number.isInteger(call.query_chars)) {
      details.push(`查询 ${call.query_chars} 字符`);
    }
    if (Number.isInteger(call.candidate_count)) {
      details.push(`候选 ${call.candidate_count} 条 / ${call.candidate_chars} 字符`);
      details.push(`ID：${call.candidate_ids.join("、")}`);
    }
    appendText(list, "li", details.join("；"));
  }
  elements.searchAudit.append(list);
}

function updateSearchButton() {
  elements.searchButton.textContent = elements.searchMode.value === "enhanced"
    ? `在所选资料库和快照中执行${enhancedRunLabel(state.enhancedSimulated)}`
    : "在所选资料库和快照中本地搜索";
  elements.searchButton.disabled =
    state.searching ||
    !state.libraryId ||
    elements.librarySelect.value !== state.libraryId ||
    !elements.query.value.trim() ||
    !elements.snapshotSelect.value ||
    (elements.searchMode.value === "enhanced" && !state.enhancedAvailable);
}

function invalidateSearch() {
  state.searchRequestId += 1;
  state.searching = false;
  elements.searchResults.replaceChildren();
  elements.searchAudit.replaceChildren();
  elements.searchAudit.hidden = true;
  setNotice(elements.searchStatus, "");
  updateSearchButton();
}

function libraryCandidateChanged() {
  renderLibraryDetails();
  renderSnapshots();
  if (elements.librarySelect.value !== state.libraryId) {
    setNotice(
      elements.libraryStatus,
      "尚未切换：请点击“明确切换”后再导入、激活或搜索。",
    );
  }
}

async function search() {
  const query = elements.query.value.trim();
  const snapshotId = elements.snapshotSelect.value;
  const mode = elements.searchMode.value;
  if (!query || !snapshotId || !state.libraryId) {
    return;
  }
  const requestedLibraryId = state.libraryId;
  const requestedEpoch = state.libraryEpoch;
  const requestId = ++state.searchRequestId;
  state.searching = true;
  updateSearchButton();
  setNotice(
    elements.searchStatus,
    mode === "enhanced"
      ? `正在先做本地完整核验，再按三角色顺序执行${enhancedRunLabel(state.enhancedSimulated)}……`
      : "正在实时核验明确快照并执行离线本地 BM25 搜索……",
  );
  try {
    const payload = await api(
      `/api/libraries/${encodeURIComponent(requestedLibraryId)}/search`,
      {
        method: "POST",
        headers: actionHeaders("search", true),
        body: JSON.stringify({
          snapshot_id: snapshotId,
          query,
          mode,
          top_k: 5,
          excerpt_chars: 1000,
        }),
      },
    );
    if (!searchRequestIsCurrent(requestId, requestedLibraryId, requestedEpoch)) {
      return;
    }
    renderResults(payload);
  } catch (error) {
    if (!searchRequestIsCurrent(requestId, requestedLibraryId, requestedEpoch)) {
      return;
    }
    elements.searchResults.replaceChildren();
    const audit = error.payload && error.payload.audit ? error.payload.audit : null;
    renderSearchAudit(audit);
    const prefix = mode === "enhanced" && audit
      ? `${enhancedRunLabel(audit.simulated)}失败：`
      : "";
    setNotice(elements.searchStatus, `${prefix}${error.message}`, true);
  } finally {
    if (searchRequestIsCurrent(requestId, requestedLibraryId, requestedEpoch)) {
      state.searching = false;
      updateSearchButton();
    }
  }
}

async function copyMcpConfig(text, label) {
  if (
    typeof text !== "string" ||
    !text ||
    !navigator.clipboard ||
    !navigator.clipboard.writeText
  ) {
    setNotice(
      elements.mcpCopyStatus,
      "浏览器未提供复制权限；请手动选择文本复制。未修改任何客户端配置。",
      true,
    );
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    setNotice(
      elements.mcpCopyStatus,
      `${label}已复制，但尚未配置；本页没有执行命令或写入客户端配置。`,
    );
  } catch (_error) {
    setNotice(
      elements.mcpCopyStatus,
      "复制未完成；请手动选择文本复制。未修改任何客户端配置。",
      true,
    );
  }
}

function renderMcpSelfCheck(report) {
  elements.mcpSelfCheckReport.replaceChildren();
  elements.mcpSelfCheckReport.hidden = false;
  appendText(
    elements.mcpSelfCheckReport,
    "h3",
    report.passed
      ? "本地离线 STDIO 自检通过"
      : "本地离线 STDIO 自检未通过",
  );
  const summary = [
    `观测范围：${report.observation_scope_zh}`,
    `工具数：${report.tool_count === null ? "未取得" : report.tool_count}`,
    `网络调用：${report.network_calls === null ? "未观测" : report.network_calls}`,
    `模型调用：${report.model_calls}`,
    `API key 使用：${report.api_keys_used}`,
    `外部配置写入：${report.external_config_writes === null ? "未观测" : report.external_config_writes}`,
    `重试：${report.retry_count}`,
  ];
  appendText(elements.mcpSelfCheckReport, "p", summary.join("；"));
  const list = document.createElement("ol");
  for (const step of Array.isArray(report.steps) ? report.steps : []) {
    appendText(
      list,
      "li",
      `${step.passed ? "通过" : "未通过"}：${step.name}。${step.message}`,
    );
  }
  elements.mcpSelfCheckReport.append(list);
}

function validMcpSelfCheckReport(report) {
  if (!report || typeof report !== "object" || Array.isArray(report)) {
    return false;
  }
  if (
    report.scope !== "local_stdio_self_check" ||
    report.evidence_level !== "offline_local_stdio" ||
    typeof report.observation_scope_zh !== "string" ||
    report.observation_scope_zh.trim().length === 0 ||
    typeof report.message !== "string" ||
    report.message.trim().length === 0 ||
    typeof report.passed !== "boolean" ||
    report.network_calls !== null ||
    report.external_config_writes !== null ||
    report.model_calls !== 0 ||
    report.api_keys_used !== 0 ||
    report.retry_count !== 0 ||
    !Array.isArray(report.steps) ||
    report.steps.length === 0
  ) {
    return false;
  }
  if (!report.steps.every((step) => (
    step &&
    typeof step === "object" &&
    !Array.isArray(step) &&
    typeof step.name === "string" &&
    step.name.trim().length > 0 &&
    typeof step.message === "string" &&
    step.message.trim().length > 0 &&
    typeof step.passed === "boolean"
  ))) {
    return false;
  }
  if (report.passed) {
    return (
      report.tool_count === 8 &&
      report.steps.length === 6 &&
      report.steps.every((step) => step.passed === true)
    );
  }
  return report.tool_count === null || report.tool_count === 8;
}

async function runMcpSelfCheck() {
  const requestId = ++state.mcpSelfCheckRequestId;
  if (state.mcpSelfCheckController) {
    state.mcpSelfCheckController.abort();
  }
  const controller = new AbortController();
  state.mcpSelfCheckController = controller;
  state.mcpSelfChecking = true;
  elements.mcpSelfCheckButton.disabled = true;
  elements.mcpSelfCheckReport.replaceChildren();
  elements.mcpSelfCheckReport.hidden = true;
  setNotice(
    elements.mcpSelfCheckStatus,
    "正在用临时合成资料启动真实本地 STDIO MCP；首错停止、不会重试……",
  );
  try {
    const payload = await api("/api/mcp-self-check", {
      method: "POST",
      headers: actionHeaders("mcp-self-check"),
      signal: controller.signal,
    });
    if (requestId !== state.mcpSelfCheckRequestId) {
      return;
    }
    const report = payload.self_check;
    if (!validMcpSelfCheckReport(report)) {
      setNotice(elements.mcpSelfCheckStatus, MCP_INVALID_REPORT_MESSAGE, true);
      return;
    }
    renderMcpSelfCheck(report);
    setNotice(
      elements.mcpSelfCheckStatus,
      report.message,
      report.passed !== true,
    );
  } catch (error) {
    if (
      error.name === "AbortError" ||
      requestId !== state.mcpSelfCheckRequestId
    ) {
      return;
    }
    setNotice(
      elements.mcpSelfCheckStatus,
      `${error.message} 未写入任何客户端配置。`,
      true,
    );
  } finally {
    if (requestId === state.mcpSelfCheckRequestId) {
      state.mcpSelfChecking = false;
      state.mcpSelfCheckController = null;
      elements.mcpSelfCheckButton.disabled = !state.mcpGuide;
    }
  }
}

async function refreshAll() {
  try {
    await loadStatus();
    if (!(await loadLibraries())) {
      return;
    }
    if (!(await loadSnapshots(elements.snapshotSelect.value))) {
      return;
    }
  } catch (error) {
    setNotice(elements.serviceStatus, error.message, true);
  }
}

elements.createLibraryButton.addEventListener("click", createLibrary);
elements.switchLibraryButton.addEventListener("click", switchLibrary);
elements.librarySelect.addEventListener("change", libraryCandidateChanged);
elements.files.addEventListener("change", () => chooseFiles(elements.files.files));
elements.buildButton.addEventListener("click", buildSnapshot);
elements.buildBlank.addEventListener("change", updateBuildControls);
elements.buildInherit.addEventListener("change", updateBuildControls);
elements.baseSnapshotSelect.addEventListener("change", updateBuildControls);
elements.refreshButton.addEventListener("click", refreshAll);
elements.searchButton.addEventListener("click", search);
elements.query.addEventListener("input", invalidateSearch);
elements.searchMode.addEventListener("change", invalidateSearch);
elements.snapshotSelect.addEventListener("change", invalidateSearch);
elements.copyMcpCliButton.addEventListener("click", () => {
  copyMcpConfig(elements.mcpCli.value, "CLI");
});
elements.copyMcpTomlButton.addEventListener("click", () => {
  copyMcpConfig(elements.mcpToml.value, "TOML");
});
elements.mcpSelfCheckButton.addEventListener("click", runMcpSelfCheck);
elements.dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  elements.dropZone.classList.add("dragging");
});
elements.dropZone.addEventListener("dragleave", () => {
  elements.dropZone.classList.remove("dragging");
});
elements.dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  elements.dropZone.classList.remove("dragging");
  chooseFiles(event.dataTransfer.files);
});

refreshAll();
