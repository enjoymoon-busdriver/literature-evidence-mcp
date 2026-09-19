"use strict";

const state = {
  csrfToken: "",
  serviceReady: false,
  status: null,
  libraries: [],
  libraryId: "",
  libraryEpoch: 0,
  snapshotId: "",
  snapshotEpoch: 0,
  snapshots: [],
  currentSnapshotId: "",
  lastSuccessfulSnapshotId: "",
  documents: [],
  selectedDocumentId: "",
  documentDetail: null,
  sectionDetail: null,
  page: "documents",
  detailsOpen: true,
  filter: "",
  mediaFilter: "all",
  managing: false,
  removalIds: [],
  removalTarget: null,
  deletionTarget: null,
  deleting: false,
  search: {busy: false, query: "", results: [], ran: false, audit: null},
  requests: {status: 0, libraries: 0, snapshots: 0, documents: 0, detail: 0, section: 0, search: 0, build: 0},
  building: false,
  buildFiles: [],
  mcpGuide: null,
  mcpSelfCheck: null,
  simulatedTunnel: null,
};

const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? "").replace(
  /[&<>"']/g,
  character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]),
);
const chip = (text, tone = "") => `<span class="chip ${tone}">${escapeHtml(text)}</span>`;
const currentLibrary = () => state.libraries.find(item => item.library_id === state.libraryId) || null;
const currentSnapshot = () => state.snapshots.find(item => item.snapshot_id === state.snapshotId) || null;
const selectedDocument = () => state.documents.find(item => item.document_id === state.selectedDocumentId) || null;
const snapshotPointerNames = {current: "当前激活", "last-successful": "上次成功"};
const narrowLayout = () => typeof window !== "undefined"
  && typeof window.matchMedia === "function"
  && window.matchMedia("(max-width: 930px)").matches;

function responseError(body, fallback) {
  if (body && typeof body.error === "string" && body.error) return body.error;
  return fallback;
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : {};
  if (!response.ok) {
    const error = new Error(responseError(body, `请求未完成（${response.status}）`));
    error.payload = body;
    throw error;
  }
  return body;
}

function writeOptions(intent, body) {
  const headers = {"X-CSRF-Token": state.csrfToken, "X-Action-Intent": intent};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return {method: "POST", headers, body: body === undefined ? undefined : JSON.stringify(body)};
}

function selectionCapture(requestId) {
  return {
    libraryId: state.libraryId,
    snapshotId: state.snapshotId,
    libraryEpoch: state.libraryEpoch,
    snapshotEpoch: state.snapshotEpoch,
    requestId,
  };
}

function selectionIsCurrent(capture, kind) {
  return capture.libraryId === state.libraryId
    && capture.snapshotId === state.snapshotId
    && capture.libraryEpoch === state.libraryEpoch
    && capture.snapshotEpoch === state.snapshotEpoch
    && capture.requestId === state.requests[kind];
}

let toastTimer;
function toast(message) {
  const element = $("#toast");
  if (!element) return;
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("visible"), 3300);
}

function heading(title, description, actions = "") {
  return `<div class="page-heading"><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div><div class="page-actions">${actions}</div></div>`;
}

function empty(title, description, action = "", symbol = "▤") {
  return `<div class="empty"><div class="empty-symbol" aria-hidden="true">${symbol}</div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(description)}</p>${action}</div>`;
}

function snapshotDate(value) {
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{6})Z-/.exec(value || "");
  return match ? new Date(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}.${match[7].slice(0, 3)}Z`) : null;
}

function shortSnapshot(value) {
  if (!value) return "—";
  const date = snapshotDate(value);
  if (!date || Number.isNaN(date.getTime())) return value;
  const minuteKey = d => [d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()].join("-");
  const peers = state.snapshots.filter(item => {
    const other = snapshotDate(item.snapshot_id);
    return other && minuteKey(other) === minuteKey(date);
  });
  const pad = n => String(n).padStart(2, "0");
  let label = `${date.getFullYear() === new Date().getFullYear() ? "" : `${date.getFullYear()} 年 `}${date.getMonth() + 1} 月 ${date.getDate()} 日 ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (peers.length > 1) {
    label += `:${pad(date.getSeconds())}`;
    const sameSecond = peers.filter(item => snapshotDate(item.snapshot_id).getSeconds() === date.getSeconds());
    if (sameSecond.length > 1) {
      let length = 4;
      while (sameSecond.some(item => item.snapshot_id !== value && item.snapshot_id.slice(-length) === value.slice(-length))) length++;
      label += ` · ${value.slice(-length)}`;
    }
  }
  return label;
}

async function copySnapshotId(snapshotId) {
  if (!state.snapshots.some(item => item.snapshot_id === snapshotId)) return;
  try {
    await navigator.clipboard.writeText(snapshotId);
    toast("完整版本 ID 已复制");
  } catch {
    toast("浏览器未允许复制，请展开版本详情手工选择完整 ID");
  }
}

function vectorStatus(snapshot) {
  return {missing: "未准备", preparing: "准备中", ready: "已就绪", mismatch: "需要重新准备", failed: "准备失败"}[snapshot.vector_status] || "未确认";
}

function documentKind(document) {
  const media = String(document.media_type || "").toLowerCase();
  if (media.includes("pdf") || String(document.source_name || "").toLowerCase().endsWith(".pdf")) return "PDF";
  return "MD";
}

function documentTitle(document) {
  return document.title || document.source_name || document.document_id;
}

function documentSubtitle(document) {
  const parts = [];
  if (document.material_type) parts.push(document.material_type);
  if (Array.isArray(document.topics) && document.topics.length) parts.push(document.topics.slice(0, 3).join(" · "));
  const chunkCount = Number.isInteger(document.chunk_count) ? document.chunk_count
    : Array.isArray(document.assets) ? document.assets.reduce((sum, asset) => sum + (asset.chunk_count || 0), 0) : null;
  if (chunkCount !== null) parts.push(`文本片段：${chunkCount} 段`);
  return parts.join(" · ") || document.document_id;
}

function clearContentSelection() {
  closeDocumentMenu();
  state.removalIds = [];
  state.managing = false;
  state.removalTarget = null;
  state.snapshotEpoch += 1;
  state.requests.detail += 1;
  state.requests.section += 1;
  state.requests.search += 1;
  state.documents = [];
  state.selectedDocumentId = "";
  state.documentDetail = null;
  state.sectionDetail = null;
  state.search = {busy: false, query: "", results: [], ran: false, audit: null};
}

async function loadStatus() {
  const requestId = ++state.requests.status;
  const connectionRevisions = globalThis.FolioConnections?.revisions?.() || null;
  try {
    const body = await requestJson("/api/status");
    if (requestId !== state.requests.status) return;
    state.status = body;
    state.csrfToken = body.csrf_token || "";
    state.serviceReady = body.service === "ready" || Boolean(body.csrf_token);
    state.mcpGuide = body.mcp_guide || null;
    const testBanner = $("#test-instance-banner");
    testBanner.hidden = state.mcpGuide?.instance_kind !== "isolated_test";
    testBanner.textContent = testBanner.hidden ? "" : `隔离测试实例：${state.mcpGuide.instance_label} · 仅使用合成资料，请勿误填正式 Key。应用根：${state.mcpGuide.application_root}`;
    if (!globalThis.FolioConnections && body.tunnel_wizard?.simulation) {
      state.simulatedTunnel = body.tunnel_wizard.simulation;
    }
    $("#service-status").textContent = state.serviceReady ? "本机服务已连接" : "本机服务未就绪";
    globalThis.FolioConnections?.acceptStatus(body, connectionRevisions);
    render();
  } catch (error) {
    if (requestId !== state.requests.status) return;
    state.serviceReady = false;
    $("#service-status").textContent = "无法连接本机服务";
    render();
  }
}

async function loadLibraries(preferredId = "") {
  const requestId = ++state.requests.libraries;
  try {
    const body = await requestJson("/api/libraries");
    if (requestId !== state.requests.libraries) return;
    state.libraries = Array.isArray(body.libraries) ? body.libraries : [];
    const preferred = state.libraries.find(item => item.library_id === preferredId);
    const selected = state.libraries.find(item => item.selected);
    const next = preferred || selected || state.libraries[0] || null;
    if (!next) {
      state.libraryId = "";
      state.libraryEpoch += 1;
      state.currentSnapshotId = "";
      state.lastSuccessfulSnapshotId = "";
      state.snapshots = [];
      state.snapshotId = "";
      clearContentSelection();
      render();
      return;
    }
    await selectLibrary(next.library_id, {persist: false});
  } catch (error) {
    toast(error.message);
    render();
  }
}

async function selectLibrary(libraryId, {persist = true} = {}) {
  if (!state.libraries.some(item => item.library_id === libraryId)) return;
  const changed = libraryId !== state.libraryId;
  if (changed) {
    state.libraryId = libraryId;
    state.libraryEpoch += 1;
    state.snapshotId = "";
    state.snapshots = [];
    state.currentSnapshotId = "";
    state.lastSuccessfulSnapshotId = "";
    clearContentSelection();
    render();
  }
  const libraryEpoch = state.libraryEpoch;
  if (persist) {
    try {
      const body = await requestJson(
        `/api/libraries/${encodeURIComponent(libraryId)}/select`,
        writeOptions("select-library"),
      );
      if (state.libraryId === libraryId && state.libraryEpoch === libraryEpoch && body.library) {
        state.libraries = state.libraries.map(item => item.library_id === libraryId
          ? {...item, ...body.library, selected: true}
          : {...item, selected: false});
      }
    } catch (error) {
      if (state.libraryId === libraryId && state.libraryEpoch === libraryEpoch) toast(error.message);
    }
  }
  if (state.libraryId === libraryId && state.libraryEpoch === libraryEpoch) await loadSnapshots();
}

async function loadSnapshots(preferredSnapshotId = "") {
  if (!state.libraryId) return;
  const libraryId = state.libraryId;
  const libraryEpoch = state.libraryEpoch;
  const requestId = ++state.requests.snapshots;
  try {
    const body = await requestJson(`/api/libraries/${encodeURIComponent(libraryId)}/snapshots`);
    if (requestId !== state.requests.snapshots || libraryId !== state.libraryId || libraryEpoch !== state.libraryEpoch) return;
    if (body.library_id !== libraryId) throw new Error("版本列表身份无效，未显示结果。");
    state.snapshots = Array.isArray(body.snapshots) ? body.snapshots : [];
    state.currentSnapshotId = body.current_snapshot_id || "";
    state.lastSuccessfulSnapshotId = body.last_successful_snapshot_id || "";
    const preferred = state.snapshots.some(item => item.snapshot_id === preferredSnapshotId) ? preferredSnapshotId : "";
    const existing = state.snapshots.some(item => item.snapshot_id === state.snapshotId) ? state.snapshotId : "";
    const next = preferred || existing || state.currentSnapshotId || state.lastSuccessfulSnapshotId || state.snapshots[0]?.snapshot_id || "";
    browseSnapshot(next, {quiet: true});
  } catch (error) {
    if (requestId === state.requests.snapshots && libraryId === state.libraryId && libraryEpoch === state.libraryEpoch) {
      state.snapshots = [];
      state.snapshotId = "";
      clearContentSelection();
      toast(error.message);
      render();
    }
  }
}

function browseSnapshot(snapshotId, {quiet = false} = {}) {
  if (snapshotId && !state.snapshots.some(item => item.snapshot_id === snapshotId)) return;
  if (snapshotId !== state.snapshotId) {
    clearContentSelection();
    state.snapshotId = snapshotId;
    const snapshot = currentSnapshot();
    state.documents = snapshot?.verified && Array.isArray(snapshot.members) ? snapshot.members : [];
    state.page = state.page === "versions" ? "documents" : state.page;
    render();
    if (snapshotId && snapshot?.verified) loadDocuments();
  } else {
    state.documents = currentSnapshot()?.verified && Array.isArray(currentSnapshot().members)
      ? currentSnapshot().members
      : [];
    render();
    if (snapshotId && currentSnapshot()?.verified) loadDocuments();
  }
  if (!quiet && snapshotId) {
    toast(snapshotId === state.currentSnapshotId
      ? "已回到当前激活版本"
      : "正在浏览历史版本；当前激活版本未改变");
  }
}

async function loadDocuments() {
  if (!state.libraryId || !state.snapshotId) return;
  const requestId = ++state.requests.documents;
  const capture = selectionCapture(requestId);
  try {
    const body = await requestJson(
      `/api/libraries/${encodeURIComponent(capture.libraryId)}/snapshots/${encodeURIComponent(capture.snapshotId)}/documents`,
    );
    if (!selectionIsCurrent(capture, "documents")) return;
    if (body.library_id !== capture.libraryId || body.snapshot_id !== capture.snapshotId) throw new Error("文档列表身份无效，未显示结果。");
    state.documents = Array.isArray(body.documents) ? body.documents : [];
    state.selectedDocumentId = state.documents[0]?.document_id || "";
    render();
    if (state.selectedDocumentId) loadDocumentDetail(state.selectedDocumentId, {open: false});
  } catch (error) {
    if (!selectionIsCurrent(capture, "documents")) return;
    state.documents = [];
    state.selectedDocumentId = "";
    toast(error.message);
    render();
  }
}

async function verifySnapshot(snapshotId) {
  if (!state.libraryId || !snapshotId) return;
  const libraryId = state.libraryId;
  const libraryEpoch = state.libraryEpoch;
  try {
    const body = await requestJson(
      `/api/libraries/${encodeURIComponent(libraryId)}/snapshots/${encodeURIComponent(snapshotId)}/verify`,
    );
    if (
      libraryId !== state.libraryId
      || libraryEpoch !== state.libraryEpoch
      || body.library_id !== libraryId
      || body.snapshot_id !== snapshotId
    ) return;
    state.snapshots = state.snapshots.map(item => item.snapshot_id === snapshotId ? {...item, ...body} : item);
    if (state.snapshotId === snapshotId) {
      state.documents = Array.isArray(body.members) ? body.members : [];
      await loadDocuments();
    }
    render();
    toast(body.verified ? "版本完整性检查通过" : "版本完整性检查未通过");
  } catch (error) {
    if (libraryId === state.libraryId && libraryEpoch === state.libraryEpoch) toast(error.message);
  }
}

async function activateSnapshot(snapshotId) {
  if (!state.libraryId || !snapshotId) return;
  const capture = {libraryId: state.libraryId, libraryEpoch: state.libraryEpoch};
  try {
    const body = await requestJson(
      `/api/libraries/${encodeURIComponent(capture.libraryId)}/snapshots/${encodeURIComponent(snapshotId)}/activate`,
      writeOptions("activate-snapshot"),
    );
    if (
      capture.libraryId !== state.libraryId
      || capture.libraryEpoch !== state.libraryEpoch
      || body.library_id !== capture.libraryId
    ) return;
    state.currentSnapshotId = body.current_snapshot_id || snapshotId;
    state.lastSuccessfulSnapshotId = body.last_successful_snapshot_id || state.lastSuccessfulSnapshotId;
    state.snapshots = state.snapshots.map(item => ({...item, current: item.snapshot_id === state.currentSnapshotId}));
    render();
    toast("当前激活版本已切换");
  } catch (error) {
    if (capture.libraryId === state.libraryId && capture.libraryEpoch === state.libraryEpoch) toast(error.message);
  }
}

async function loadDocumentDetail(documentId, {open = true, chunkId = ""} = {}) {
  if (!state.libraryId || !state.snapshotId || !documentId) return;
  state.selectedDocumentId = documentId;
  state.search.selectedChunkId = chunkId;
  document.querySelectorAll('[data-action="select-document"]').forEach(item => {
    const selected = item.dataset.id === documentId
      && (item.dataset.chunkId === undefined || Boolean(chunkId) && item.dataset.chunkId === chunkId);
    item.classList.toggle("selected", selected);
    if (item.dataset.chunkId !== undefined) item.setAttribute("aria-pressed", String(selected));
  });
  state.documentDetail = null;
  state.sectionDetail = null;
  state.detailsOpen = open || !narrowLayout();
  const requestId = ++state.requests.detail;
  const capture = selectionCapture(requestId);
  renderDetails();
  try {
    const body = await requestJson(
      `/api/libraries/${encodeURIComponent(capture.libraryId)}/snapshots/${encodeURIComponent(capture.snapshotId)}/documents/${encodeURIComponent(documentId)}`,
    );
    if (!selectionIsCurrent(capture, "detail") || state.selectedDocumentId !== documentId) return;
    if (body.library_id !== capture.libraryId || body.snapshot_id !== capture.snapshotId) throw new Error("文档详情身份无效，未显示结果。");
    state.documentDetail = body;
    renderDetails();
  } catch (error) {
    if (!selectionIsCurrent(capture, "detail") || state.selectedDocumentId !== documentId) return;
    state.documentDetail = {error: error.message};
    renderDetails();
  }
}

async function loadSection(sectionId) {
  if (!state.libraryId || !state.snapshotId || !state.selectedDocumentId || !sectionId) return;
  const documentId = state.selectedDocumentId;
  state.sectionDetail = {loading: true};
  const requestId = ++state.requests.section;
  const capture = selectionCapture(requestId);
  renderDetails();
  try {
    const body = await requestJson(
      `/api/libraries/${encodeURIComponent(capture.libraryId)}/snapshots/${encodeURIComponent(capture.snapshotId)}/documents/${encodeURIComponent(documentId)}/sections/${encodeURIComponent(sectionId)}`,
    );
    if (!selectionIsCurrent(capture, "section") || state.selectedDocumentId !== documentId) return;
    if (body.library_id !== capture.libraryId || body.snapshot_id !== capture.snapshotId || body.document_id !== documentId) throw new Error("证据段落身份无效，未显示结果。");
    state.sectionDetail = body;
    renderDetails();
  } catch (error) {
    if (!selectionIsCurrent(capture, "section") || state.selectedDocumentId !== documentId) return;
    state.sectionDetail = {error: error.message};
    renderDetails();
  }
}

async function runSearch(query = state.search.query) {
  const normalized = String(query || "").trim();
  if (!state.libraryId || !state.snapshotId) return toast("请先选择资料库和版本");
  if (!normalized) return toast("请输入问题或关键词");
  state.search = {...state.search, busy: true, query: normalized, selectedChunkId: "", results: [], ran: true, audit: null, error: ""};
  state.selectedDocumentId = "";
  state.documentDetail = null;
  state.sectionDetail = null;
  const requestId = ++state.requests.search;
  const capture = selectionCapture(requestId);
  render();
  const mode = globalThis.FolioConnections?.enhancedEnabled() ? "enhanced" : "bm25";
  try {
    const body = await requestJson(
      `/api/libraries/${encodeURIComponent(capture.libraryId)}/search`,
      writeOptions("search", {snapshot_id: capture.snapshotId, query: normalized, top_k: 10, excerpt_chars: 1000, mode}),
    );
    if (!selectionIsCurrent(capture, "search")) return;
    if (body.library_id !== capture.libraryId || body.snapshot_id !== capture.snapshotId) throw new Error("搜索结果身份无效，未显示结果。");
    state.search = {...state.search, busy: false, results: Array.isArray(body.results) ? body.results : [], audit: body.provider_audit || body.audit || null, error: ""};
    const first = state.search.results[0];
    state.selectedDocumentId = first?.document_id || "";
    render();
    if (state.selectedDocumentId) loadDocumentDetail(state.selectedDocumentId, {open: false});
  } catch (error) {
    if (!selectionIsCurrent(capture, "search")) return;
    state.search = {...state.search, busy: false, results: [], audit: error.payload?.provider_audit || error.payload?.audit || null, error: error.message};
    toast(error.message);
    render();
  }
}

function invalidateSearch(query = "") {
  state.requests.search += 1;
  state.search = {busy: false, query, selectedChunkId: "", results: [], ran: false, audit: null, error: ""};
  state.selectedDocumentId = "";
  state.documentDetail = null;
  state.sectionDetail = null;
  render();
}

function filteredDocuments() {
  return state.documents.filter(document => {
    const kindMatch = state.mediaFilter === "all" || documentKind(document) === state.mediaFilter;
    const haystack = `${documentTitle(document)} ${document.source_name || ""} ${documentSubtitle(document)}`.toLowerCase();
    return kindMatch && haystack.includes(state.filter.toLowerCase());
  });
}

let documentMenuTarget = null;

function closeDocumentMenu(restoreFocus = false) {
  const target = documentMenuTarget;
  documentMenuTarget = null;
  const menu = $("#document-menu");
  if (menu) menu.hidden = true;
  if (restoreFocus) target?.row.focus();
}

function openDocumentMenu(row, x, y) {
  const documentId = row.dataset.id;
  const item = state.documents.find(document => document.document_id === documentId);
  if (!item || state.page !== "documents") return;
  closeDocumentMenu();
  documentMenuTarget = {...selectionCapture(), documentId, row};
  const menu = $("#document-menu");
  $("#document-menu-title").textContent = documentTitle(item);
  menu.querySelector('[data-action="context-remove"]').disabled = state.documents.length <= 1 || !currentSnapshot()?.verified || state.building;
  menu.hidden = false;
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - bounds.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - bounds.height - 8))}px`;
  menu.querySelector('[role="menuitem"]').focus();
}

function documentMenuAction(action) {
  const target = documentMenuTarget;
  closeDocumentMenu();
  if (!target || target.libraryId !== state.libraryId || target.snapshotId !== state.snapshotId
      || target.libraryEpoch !== state.libraryEpoch || target.snapshotEpoch !== state.snapshotEpoch) return;
  if (action === "context-details") loadDocumentDetail(target.documentId);
  else removalModal([target.documentId], true);
}

function documentVectorBadge(document) {
  const label = {ready: "已向量化", missing: "未向量化", mismatch: "需重新准备"}[document.vector_status] || "未确认";
  const coverage = Number.isInteger(document.vector_total_chunks)
    ? `当前配置已覆盖 ${document.vector_covered_chunks}/${document.vector_total_chunks} 段文本片段` : "向量覆盖情况未确认";
  return `<span class="document-vector-status" title="${escapeHtml(coverage)}">${escapeHtml(label)}</span>`;
}

function documentsPage() {
  const library = currentLibrary();
  const snapshot = currentSnapshot();
  const actions = `<button type="button" data-action="manage-documents" ${!snapshot?.verified ? "disabled" : ""}>${state.managing ? "退出管理" : "批量管理"}</button><button type="button" data-action="library-settings">库设置</button><button type="button" data-action="toggle-details">${state.detailsOpen ? "收起" : "展开"}详情</button>`;
  let html = heading("文档", "把文献、来源和证据放在同一个地方。", actions);
  if (!library) return html + empty("还没有文献库", "新建一个文献库，然后导入 Markdown 或带文本层 PDF。", '<button class="primary" type="button" data-action="new-library">＋ 新建文献库</button>');
  if (!snapshot) return html + empty("这个库还没有版本", "导入文档会创建第一个完整冻结版本。", '<button class="primary" type="button" data-action="import">＋ 导入文档</button>');
  const docs = filteredDocuments();
  const pdfCount = state.documents.filter(item => documentKind(item) === "PDF").length;
  html += `<div class="summary-strip"><div><b>${state.documents.length}</b> 篇文档</div><div><b>${pdfCount}</b> 份 PDF</div><div>文本搜索 ${chip(snapshot.verified ? "可用" : "不可用", snapshot.verified ? "green" : "red")}</div><div>版本完整性 ${chip(snapshot.verified ? "检查通过" : "检查失败", snapshot.verified ? "green" : "red")}</div></div>`;
  if (!snapshot.verified) return html + empty("这个版本未通过完整性检查", "请在版本历史中检查版本完整性；检查通过前不能浏览内容、搜索或继承导入。", '<button type="button" data-action="nav" data-page="versions">查看版本历史</button>', "!");
  html += `<div class="toolbar"><label class="sr-only" for="document-filter">筛选文档</label><input id="document-filter" value="${escapeHtml(state.filter)}" placeholder="筛选标题或文件名…"><label class="sr-only" for="kind-filter">格式</label><select id="kind-filter"><option value="all">全部格式</option><option value="PDF" ${state.mediaFilter === "PDF" ? "selected" : ""}>PDF</option><option value="MD" ${state.mediaFilter === "MD" ? "selected" : ""}>Markdown</option></select><small>浏览版本 ${escapeHtml(shortSnapshot(state.snapshotId))}</small></div>`;
  if (state.managing) html += `<div class="batch-toolbar"><strong>已选 ${state.removalIds.length} 篇</strong><button type="button" data-action="select-filtered">全选当前筛选结果（${docs.length} 篇）</button><button type="button" data-action="clear-selection">取消选择</button><button type="button" data-action="remove-documents" ${!state.removalIds.length || state.removalIds.length >= state.documents.length || state.building ? "disabled" : ""}>从新版本中移除（${state.removalIds.length} 篇）</button><small>更改筛选、资料库或浏览版本会清空选择。${state.removalIds.length >= state.documents.length ? "每个版本至少保留一篇文档，不能移除全部文档或最后一篇。" : ""}</small></div>`;
  if (!state.documents.length) return html + empty("这个版本没有文档", "可从当前版本导入文件，形成新的完整版本。", '<button class="primary" type="button" data-action="import">＋ 导入文档</button>');
  if (!docs.length) return html + empty("没有匹配的文档", "换一个关键词或清除筛选。", '<button type="button" data-action="clear-filter">清除筛选</button>', "⌕");
  return html + `<div class="document-list"><div class="list-header"><span>标题与来源</span><span>格式</span></div>${docs.map(document => `<div class="document-entry">${state.managing ? `<input type="checkbox" data-removal-id="${escapeHtml(document.document_id)}" aria-label="选择移除 ${escapeHtml(documentTitle(document))}" ${state.removalIds.includes(document.document_id) ? "checked" : ""}>` : ""}<button class="doc-row ${document.document_id === state.selectedDocumentId ? "selected" : ""}" type="button" aria-haspopup="menu" data-action="select-document" data-id="${escapeHtml(document.document_id)}"><span class="file-icon">${documentKind(document)}</span><span class="row-text"><strong class="doc-title">${escapeHtml(documentTitle(document))} ${documentVectorBadge(document)}</strong><span class="doc-sub">${escapeHtml(documentSubtitle(document))}</span></span><span class="row-kind">${documentKind(document)}</span></button></div>`).join("")}</div><div class="note-box">每份文档都属于当前浏览版本。选择文档可查看元数据、目录和证据定位。</div>`;
}

function removalModal(ids = state.removalIds, singleContext = false) {
  const documents = state.documents.filter(item => ids.includes(item.document_id));
  if (!documents.length || documents.length >= state.documents.length || state.building) return;
  state.removalTarget = {...selectionCapture(), ids: documents.map(item => item.document_id)};
  showModal("从新版本中移除文档", `<p>所属库：${escapeHtml(currentLibrary()?.name)}<br>基础浏览版本：${escapeHtml(shortSnapshot(state.snapshotId))}</p><details><summary>完整基础版本 ID</summary><p>${escapeHtml(state.snapshotId)}</p></details>${singleContext ? "<p>本次仅移除右键指定的这一篇，不包含批量勾选的其他文档。</p>" : ""}<p>将移除 ${documents.length} 篇：</p><ul>${documents.map(item => `<li>${escapeHtml(documentTitle(item))} · ${escapeHtml(item.source_name || item.document_id)}</li>`).join("")}</ul><div class="note-box">只创建不含这些文档的新版本。旧版本与电脑原始文件不删除，当前激活版本不会自动切换，也不会立即释放被旧版本引用的空间。</div>`, '<button type="button" data-action="close-modal">取消</button><button class="primary" type="button" data-action="confirm-remove">确认创建新版本</button>');
}

async function removeDocuments() {
  const target = state.removalTarget;
  if (!target || state.building || target.libraryId !== state.libraryId || target.snapshotId !== state.snapshotId || target.libraryEpoch !== state.libraryEpoch || target.snapshotEpoch !== state.snapshotEpoch) return;
  const capture = {...target, requestId: ++state.requests.build};
  state.building = true;
  const button = document.querySelector('[data-action="confirm-remove"]');
  if (button) { button.disabled = true; button.textContent = "正在创建新版本…"; }
  try {
    const body = await requestJson(`/api/libraries/${encodeURIComponent(capture.libraryId)}/remove-documents`, writeOptions("remove-documents", {base_snapshot_id: capture.snapshotId, remove_document_ids: capture.ids}));
    if (!selectionIsCurrent(capture, "build")) return;
    if (body.library_id !== capture.libraryId || !body.published || !body.snapshot_id) throw new Error("新版本结果未确认，请查看版本历史。");
    state.removalIds = [];
    state.removalTarget = null;
    await loadSnapshots(capture.snapshotId);
    const counts = body.difference?.counts || {};
    showModal("新版本已创建", `<p>已从新版本 ${escapeHtml(shortSnapshot(body.snapshot_id))} 中移除 ${counts.removed ?? capture.ids.length} 篇文档。</p><p>新增 ${counts.added ?? 0} · 继承 ${counts.inherited ?? 0} · 移除 ${counts.removed ?? capture.ids.length}</p><p>旧版本和原始文件保留，当前激活版本保持不变。</p>`, `<button type="button" data-action="close-modal">关闭</button><button class="primary" type="button" data-action="view-removed-version" data-id="${escapeHtml(body.snapshot_id)}">查看新版本</button>`);
  } catch (error) {
    if (!selectionIsCurrent(capture, "build")) return;
    toast(error.message);
    if (button) { button.disabled = false; button.textContent = "确认创建新版本"; }
  } finally {
    state.building = false;
    render();
  }
}

function searchPage() {
  const enhanced = globalThis.FolioConnections?.enhancedEnabled();
  let html = heading("搜索", "在当前库和当前浏览版本中寻找可追溯证据。", `<button type="button" data-action="toggle-details">${state.detailsOpen ? "收起" : "展开"}详情</button>`);
  html += `<form id="search-form" class="search-box"><label class="sr-only" for="search-input">搜索文献</label><input id="search-input" maxlength="400" autocomplete="off" value="${escapeHtml(state.search.query)}" placeholder="输入问题或关键词…" ${state.search.busy ? "disabled" : ""}><select id="search-mode" aria-label="搜索模式"><option value="bm25" ${enhanced ? "" : "selected"}>BM25（离线、零密钥、零模型调用）</option><option value="enhanced" ${enhanced ? "selected" : ""} ${globalThis.FolioConnections?.enhancedAvailable() ? "" : "disabled"}>${globalThis.FolioConnections?.enhancedAvailable() ? "AI 增强搜索" : "增强搜索（当前不可用）"}</option></select><button class="primary" type="submit" ${state.search.busy || !state.snapshotId || !currentSnapshot()?.verified ? "disabled" : ""}>${state.search.busy ? "搜索中…" : "搜索"}</button></form><div class="search-hints">${chip(enhanced ? "AI 增强" : "本地 BM25")}<span>${enhanced ? "最多三次模型请求，零自动重试" : "离线、零模型调用"}</span></div>`;
  if (!state.snapshotId) return html + empty("没有可搜索的版本", "先导入文档建立版本。", "", "⌕");
  if (!currentSnapshot()?.verified) return html + empty("当前浏览版本未通过完整性检查", "请先到版本历史检查版本完整性。", '<button type="button" data-action="nav" data-page="versions">查看版本历史</button>', "!");
  if (state.search.busy) return html + empty("正在搜索…", enhanced ? "正在按顺序执行增强检索。" : "正在本机执行 BM25 检索。", "", "◌");
  if (!state.search.ran) return html + empty("从一个问题或关键词开始", "结果会展示证据摘录和精确出处。", "", "⌕");
  if (state.search.error) {
    const calls = state.search.audit?.call_count;
    return html + `<div class="note-box danger"><strong>搜索未完成</strong><p>${escapeHtml(state.search.error)}</p>${Number.isInteger(calls) ? `<p>实际已发送 ${calls} 次模型请求，可能计费；没有自动重试。</p>` : ""}</div>`;
  }
  if (state.search.audit) html += `<div class="note-box">${state.search.audit.simulated === true ? "离线模拟增强调用审计" : "增强调用审计"}：实际 ${state.search.audit.call_count ?? 0} 次；零自动重试。</div>`;
  if (!state.search.results.length) return html + empty("没有找到相关证据", "本次搜索未返回证据，不代表资料库中不存在。", "", "⌕");
  html += `<div class="search-hints"><strong>找到 ${state.search.results.length} 条结果</strong><span>${escapeHtml(shortSnapshot(state.snapshotId))}</span></div>`;
  return html + state.search.results.map(result => `<button class="search-card ${result.chunk_id && result.chunk_id === state.search.selectedChunkId && result.document_id === state.selectedDocumentId ? "selected" : ""}" type="button" data-action="select-document" data-id="${escapeHtml(result.document_id)}" data-chunk-id="${escapeHtml(result.chunk_id || "")}" aria-pressed="${Boolean(result.chunk_id && result.chunk_id === state.search.selectedChunkId && result.document_id === state.selectedDocumentId)}">${chip(documentKind(result))}<h3>${escapeHtml(documentTitle(result))}</h3><small>${escapeHtml(result.source_name || result.document_id)}</small><p>${escapeHtml(result.excerpt || "")}</p><div class="anchor">${escapeHtml(result.anchor_label || "证据块 " + (result.chunk_id || ""))}</div></button>`).join("");
}

function versionsPage() {
  let html = heading("版本历史", "浏览旧版本不会改变当前激活版本；激活始终需要明确操作。");
  if (!state.libraryId) return html + empty("还没有文献库", "新建资料库后，版本会显示在这里。", "", "◷");
  if (!state.snapshots.length) return html + empty("还没有成功版本", "首次成功导入会同时成为当前激活版本和上次成功版本。", "", "◷");
  html += state.snapshots.map(snapshot => {
    const difference = snapshot.difference?.counts;
    const diff = difference ? `新增 ${difference.added} · 继承 ${difference.inherited} · 替换 ${difference.replaced} · 移除 ${difference.removed}` : "无可用差异统计";
    return `<article class="version-card"><div class="version-marker">V</div><div class="version-info"><h3>${escapeHtml(shortSnapshot(snapshot.snapshot_id))} ${snapshot.snapshot_id === state.currentSnapshotId ? chip(snapshotPointerNames.current, "green") : ""} ${snapshot.snapshot_id === state.lastSuccessfulSnapshotId ? chip(snapshotPointerNames["last-successful"]) : ""} ${snapshot.snapshot_id === state.snapshotId ? chip("正在浏览", "amber") : ""}</h3><details class="version-id"><summary>版本详情</summary><p>${escapeHtml(snapshot.snapshot_id)}</p><button type="button" data-action="copy-snapshot-id" data-id="${escapeHtml(snapshot.snapshot_id)}">复制完整 ID</button></details><p>${snapshot.members?.length || 0} 篇文档 · ${escapeHtml(diff)} · 向量：${vectorStatus(snapshot)} · ${snapshot.verified ? "版本完整性检查通过" : "版本完整性检查失败"}</p></div><div class="version-actions"><button type="button" data-action="browse-version" data-id="${escapeHtml(snapshot.snapshot_id)}">浏览文档</button><button type="button" data-action="verify-version" data-id="${escapeHtml(snapshot.snapshot_id)}">检查版本完整性</button><button type="button" data-action="activate-version" data-id="${escapeHtml(snapshot.snapshot_id)}" ${snapshot.snapshot_id === state.currentSnapshotId || !snapshot.verified ? "disabled" : ""}>设为当前版本</button><button type="button" data-action="vector-version" data-id="${escapeHtml(snapshot.snapshot_id)}" ${snapshot.verified ? "" : "disabled"}>准备向量</button></div></article>`;
  }).join("");
  return html + '<div class="note-box">首次成功版本自动成为当前版本。此后的成功导入只推进“上次成功”，不会自动改变当前激活版本。</div>';
}

function settingsPage() {
  if (globalThis.FolioConnections?.renderSettings) return globalThis.FolioConnections.renderSettings();
  return heading("全局设置", "正在读取模型配置…") + empty("设置正在载入", "请稍候。", "", "⚙");
}

function appsPage() {
  if (globalThis.FolioConnections?.renderApps) return globalThis.FolioConnections.renderApps();
  return heading("连接 AI 应用", "正在读取连接状态…") + empty("连接设置正在载入", "请稍候。", "", "↗");
}

function helpPage() {
  return heading("让文献整理，从容一点", "先建库、再导入、然后带着出处寻找证据。") + `<div class="help-steps"><article class="help-step"><b>01</b><h3>建立文献库</h3><p>用库区分不同课题。切库会立即清空上一库的文档、详情和搜索结果。</p></article><article class="help-step"><b>02</b><h3>导入形成版本</h3><p>每次成功导入都创建新的完整冻结版本，旧版本保持不变。</p></article><article class="help-step"><b>03</b><h3>搜索与核对出处</h3><p>默认 BM25 全程本地；证据摘录保留页码、标题路径或行号定位。</p></article></div><div class="note-box">FolioHook 的八个 MCP 工具只读。导入、激活、保存设置和连接操作始终需要在管理页明确触发。</div>`;
}

function renderDetails() {
  const details = $("#details");
  if (!details) return;
  const visible = state.detailsOpen
    && ["documents", "search"].includes(state.page)
    && (state.selectedDocumentId || !narrowLayout());
  details.hidden = !visible;
  if (!visible) return;
  const summary = selectedDocument();
  let html = '<div class="detail-heading"><h3>文档详情</h3><button class="quiet" type="button" data-action="toggle-details" aria-label="收起文档详情">✕</button></div>';
  if (!summary) {
    details.innerHTML = html + empty("选择一份文档", "来源、目录与证据定位会显示在这里。", "", "▤");
    return;
  }
  if (!state.documentDetail) {
    details.innerHTML = html + `<div class="detail-icon">${documentKind(summary)}</div><h2 class="detail-title">${escapeHtml(documentTitle(summary))}</h2><p class="muted" style="margin-top:12px">正在读取文档详情…</p>`;
    return;
  }
  if (state.documentDetail.error) {
    details.innerHTML = html + `<div class="detail-icon">${documentKind(summary)}</div><h2 class="detail-title">${escapeHtml(documentTitle(summary))}</h2><div class="note-box danger">${escapeHtml(state.documentDetail.error)}</div>`;
    return;
  }
  const document = state.documentDetail.document || summary;
  const assets = Array.isArray(document.assets) ? document.assets : [];
  const totalChunks = assets.reduce((sum, item) => sum + (Number.isInteger(item.chunk_count) ? item.chunk_count : 0), 0);
  const totalPages = assets.reduce((sum, item) => sum + (Number.isInteger(item.page_count) ? item.page_count : 0), 0);
  const toc = Array.isArray(state.documentDetail.toc?.items) ? state.documentDetail.toc.items : [];
  html += `<div class="detail-icon">${documentKind(document)}</div><h2 class="detail-title">${escapeHtml(documentTitle(document))}</h2><p class="muted" style="margin-top:10px;font-size:12px">${escapeHtml(document.source_name || document.document_id)}</p><dl class="detail-meta"><dt>格式</dt><dd>${documentKind(document)}</dd><dt>文本片段</dt><dd>${totalChunks ? `${totalChunks} 段` : "—"}</dd><dt>页数</dt><dd>${totalPages || "—"}</dd><dt>浏览版本</dt><dd>${chip(shortSnapshot(state.snapshotId))}</dd><dt>文本核验</dt><dd>${document.fulltext_verified ? chip("已人工核验", "green") : chip("未人工核验", "amber")}</dd><dt>公式核验</dt><dd>${document.formula_verified ? chip("已人工核验", "green") : chip("未人工核验", "amber")}</dd></dl>`;
  html += `<div class="detail-section"><h3>文档导航</h3>${toc.length ? `<div class="toc">${toc.map(item => `<button type="button" data-action="read-section" data-id="${escapeHtml(item.section_id)}">${escapeHtml(item.label || item.heading || item.anchor_label || item.section_id)}</button>`).join("")}</div>` : '<p class="muted" style="margin-top:12px;font-size:12px">没有可用导航项。</p>'}</div>`;
  if (state.sectionDetail?.loading) html += '<div class="detail-section"><p class="muted">正在读取证据段落…</p></div>';
  else if (state.sectionDetail?.error) html += `<div class="detail-section"><p class="danger">${escapeHtml(state.sectionDetail.error)}</p></div>`;
  else if (state.sectionDetail) {
    const results = Array.isArray(state.sectionDetail.results) ? state.sectionDetail.results : [];
    html += `<div class="detail-section"><h3>${escapeHtml(state.sectionDetail.section?.label || "证据段落")}</h3>${results.map(result => `<div class="evidence">${escapeHtml(result.excerpt || result.text || "")}<small>${escapeHtml(result.anchor_label || result.chunk_id || "")}</small></div>`).join("") || '<p class="muted" style="margin-top:12px">此导航项没有返回证据。</p>'}</div>`;
  }
  details.innerHTML = html;
}

function render() {
  closeDocumentMenu();
  const library = currentLibrary();
  const snapshot = currentSnapshot();
  $("#library-count").textContent = state.libraries.length ? String(state.libraries.length) : "";
  const select = $("#library-select");
  select.disabled = !state.libraries.length;
  select.innerHTML = state.libraries.length
    ? state.libraries.map(item => `<option value="${escapeHtml(item.library_id)}" ${item.library_id === state.libraryId ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")
    : "<option>尚无文献库</option>";
  $("#current-library").textContent = library?.name || "尚未选择";
  $("#current-library").title = library?.name || "";
  $("#view-version").textContent = shortSnapshot(state.snapshotId);
  $("#view-version").title = state.snapshotId;
  $("#active-version").textContent = shortSnapshot(state.currentSnapshotId);
  $("#active-version").title = state.currentSnapshotId;
  $("#document-count").textContent = state.documents.length ? String(state.documents.length) : "";
  document.querySelectorAll("#navigation button").forEach(button => {
    const active = button.dataset.page === state.page;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("#historical-banner").innerHTML = state.snapshotId && state.snapshotId !== state.currentSnapshotId
    ? `<div class="historical"><span>正在浏览非当前版本 ${escapeHtml(shortSnapshot(state.snapshotId))}；当前激活版本仍是 ${escapeHtml(shortSnapshot(state.currentSnapshotId))}</span><button type="button" data-action="return-active">回到当前版本</button></div>`
    : "";
  const page = ({documents: documentsPage, search: searchPage, versions: versionsPage, settings: settingsPage, apps: appsPage, help: helpPage}[state.page] || documentsPage);
  $("#content").innerHTML = page();
  $("#footer-status").textContent = state.building ? "正在本机构建完整版本…" : (snapshot?.verified === false ? "当前浏览版本完整性检查失败" : "本地 BM25 搜索无需 AI Key");
  renderDetails();
}

function showModal(title, body, actions) {
  $("#modal-content").innerHTML = `<div class="modal-heading"><h2 id="modal-title">${escapeHtml(title)}</h2><button class="quiet" type="button" data-action="close-modal" aria-label="关闭弹窗">✕</button></div><div class="modal-body">${body}</div><div class="modal-actions">${actions}</div>`;
  const modal = $("#modal");
  if (!modal.open) modal.showModal();
}

function libraryModal(libraryId = "") {
  const library = state.libraries.find(item => item.library_id === libraryId);
  showModal(
    library ? "库设置" : "新建文献库",
    `<p>${library ? "修改名称或描述不会改变稳定的资料库 ID。" : "创建一个相互隔离的空白资料库。"}</p><label class="field">库名称<input id="library-name" maxlength="120" value="${escapeHtml(library?.name || "")}" placeholder="例如：我的研究课题"></label><label class="field">描述（选填）<textarea id="library-description" rows="3" maxlength="1000">${escapeHtml(library?.description || "")}</textarea></label><p id="library-error" class="danger" role="alert"></p>`,
    `${library ? `<button class="danger" type="button" data-action="delete-library-dialog" data-id="${escapeHtml(libraryId)}">永久删除文献库</button>` : ""}<button type="button" data-action="close-modal">取消</button><button class="primary" type="button" data-action="save-library" data-id="${escapeHtml(libraryId)}">${library ? "保存库信息" : "创建文献库"}</button>`,
  );
}

async function deleteLibraryDialog(libraryId) {
  if (state.deleting || state.building) return;
  try {
    const listing = await requestJson("/api/libraries");
    const library = listing.libraries?.find(item => item.library_id === libraryId);
    if (!library) throw new Error("找不到指定文献库，请重新打开管理库。");
    const versions = await requestJson(`/api/libraries/${encodeURIComponent(libraryId)}/snapshots`);
    if (versions.library_id !== libraryId) throw new Error("库身份未确认，不能删除。");
    const current = versions.snapshots?.find(item => item.snapshot_id === versions.current_snapshot_id);
    const count = current?.verified ? current.members.length : versions.current_snapshot_id ? "未确认" : 0;
    state.deletionTarget = {libraryId, name: library.name};
    showModal("永久删除文献库", `<p class="danger"><strong>将永久删除「${escapeHtml(library.name)}」，不可恢复。</strong></p><details open><summary>目标库 ID（同名库请核对）</summary><p>${escapeHtml(libraryId)}</p></details><p>${versions.snapshots.length} 个历史版本；${count} 篇文档（仅计当前激活版本）。</p><p>删除范围：本库管理的文档副本、派生文本、数据库/索引、所有历史版本、对象存储、向量与本库元数据。</p><p>不删除导入前的外部原始文件，不影响其他库，不清除应用级共享模型 Key 和连接配置。</p><label class="field">输入准确库名以确认<input id="delete-library-name" autocomplete="off" value=""></label><p id="delete-library-error" class="danger" role="alert"></p>`, '<button type="button" data-action="close-modal">取消</button><button class="danger" type="button" data-action="confirm-delete-library" disabled>永久删除</button>');
  } catch (error) { toast(error.message); }
}

async function deleteLibrary() {
  const target = state.deletionTarget;
  if (!target || state.deleting || state.building || $("#delete-library-name")?.value !== target.name) return;
  state.deleting = true;
  const button = document.querySelector('[data-action="confirm-delete-library"]');
  if (button) button.disabled = true;
  try {
    const body = await requestJson(`/api/libraries/${encodeURIComponent(target.libraryId)}/delete`, writeOptions("delete-library", {confirmation_name: target.name}));
    if (body.deleted !== true || body.deleted_library_id !== target.libraryId) throw new Error("删除结果未确认，请检查管理库列表。");
    if (state.libraryId === target.libraryId) {
      state.libraryId = "";
      state.libraryEpoch += 1;
      state.snapshotId = "";
      state.snapshots = [];
      state.currentSnapshotId = "";
      state.lastSuccessfulSnapshotId = "";
      clearContentSelection();
    }
    state.deletionTarget = null;
    $("#modal").close();
    await loadLibraries(state.libraryId);
    toast("文献库已永久删除");
  } catch (error) {
    // A cleanup failure can occur after unregistering; refresh discovery, never report success.
    await loadLibraries(state.libraryId);
    const message = $("#delete-library-error");
    if (message) message.textContent = error.message;
    if (button) button.disabled = !state.libraries.some(item => item.library_id === target.libraryId);
  } finally {
    state.deleting = false;
    render();
  }
}

function manageLibrariesModal() {
  const rows = state.libraries.map(library => `<div class="library-manage-row"><div><strong>${escapeHtml(library.name)}</strong><small>${library.snapshot_count || 0} 个版本 · ${escapeHtml(library.description || "无描述")}</small></div><button type="button" data-action="edit-library" data-id="${escapeHtml(library.library_id)}">库设置</button></div>`).join("");
  showModal("管理文献库", rows || "<p>还没有文献库。</p>", '<button type="button" data-action="close-modal">完成</button><button class="primary" type="button" data-action="new-library">＋ 新建库</button>');
}

function importModal() {
  if (!state.libraryId) return libraryModal();
  const base = currentSnapshot()?.verified ? state.snapshotId : "";
  showModal(
    "导入文档",
      `<p>导入到「${escapeHtml(currentLibrary()?.name || "")}」。成功后创建不可变的新版本，旧版本保留。</p><label class="field">建立方式<select id="build-mode"><option value="inherit" ${base ? "selected" : "disabled"}>基于当前浏览版本继续</option><option value="blank" ${base ? "" : "selected"}>从空白建立</option></select></label><label id="drop-zone" class="sample-file" for="source-files"><span>▤</span><div><strong>拖入或选择 Markdown / 带文本层 PDF</strong><small>文件只发送到本机 127.0.0.1</small></div></label><input id="source-files" class="sr-only" type="file" accept=".md,.markdown,.pdf,text/markdown,application/pdf" multiple><div id="selected-files" class="note-box">${state.buildFiles.length ? state.buildFiles.map(file => `${escapeHtml(file.name)} · 待提交`).join("<br>") : "尚未选择文件。"}</div><p id="build-error" class="danger" role="alert"></p>`,
    '<button type="button" data-action="close-modal">取消</button><button class="primary" type="button" data-action="start-import">明确导入并建立版本</button>',
  );
}

async function saveLibrary(libraryId) {
  const name = $("#library-name").value.trim();
  const description = $("#library-description").value.trim();
  if (!name) return void ($("#library-error").textContent = "请填写库名称。");
  const button = document.querySelector('[data-action="save-library"]');
  button.disabled = true;
  try {
    if (!libraryId) {
      const body = await requestJson("/api/libraries", writeOptions("create-library", {name, description}));
      $("#modal").close();
      await loadLibraries(body.library?.library_id || "");
      toast("文献库已创建");
      return;
    }
    const original = state.libraries.find(item => item.library_id === libraryId);
    if (original?.name !== name) await requestJson(`/api/libraries/${encodeURIComponent(libraryId)}/rename`, writeOptions("rename-library", {name}));
    if ((original?.description || "") !== description) await requestJson(`/api/libraries/${encodeURIComponent(libraryId)}/description`, writeOptions("update-library-description", {description}));
    $("#modal").close();
    await loadLibraries(libraryId);
    toast("库信息已保存");
  } catch (error) {
    $("#library-error").textContent = error.message;
    button.disabled = false;
  }
}

async function startImport() {
  const files = state.buildFiles.length ? state.buildFiles : Array.from($("#source-files").files || []);
  if (!files.length) return void ($("#build-error").textContent = "请至少选择一个 Markdown 或 PDF 文件。");
  const libraryId = state.libraryId;
  const libraryEpoch = state.libraryEpoch;
  const requestId = ++state.requests.build;
  const baseSnapshotId = state.snapshotId;
  const mode = $("#build-mode").value;
  const form = new FormData();
  form.append("build_mode", mode);
  if (mode === "inherit") form.append("base_snapshot_id", baseSnapshotId);
  files.forEach(file => form.append("files", file, file.name));
  const button = document.querySelector('[data-action="start-import"]');
  button.disabled = true;
  state.building = true;
  $("#selected-files").textContent = files.map(file => `${file.name} · 本机接收 / 构建中`).join("\n");
  try {
    const body = await requestJson(`/api/libraries/${encodeURIComponent(libraryId)}/build`, {
      method: "POST",
      headers: {"X-CSRF-Token": state.csrfToken, "X-Action-Intent": "build-snapshot"},
      body: form,
    });
    if (libraryId !== state.libraryId || libraryEpoch !== state.libraryEpoch) return;
    if (body.library_id !== libraryId) throw new Error("构建结果身份无效，未切换版本。");
    await loadSnapshots(body.snapshot_id || body.last_successful_snapshot_id || "");
    if (requestId !== state.requests.build || libraryId !== state.libraryId || libraryEpoch !== state.libraryEpoch) return;
    state.page = "documents";
    render();
    const fileRows = (body.files || []).map(item => `<div class="connection-result"><strong>${escapeHtml(item.name)}</strong> · ${escapeHtml(item.stage || "已进入成功快照")}</div>`).join("");
    const counts = body.difference?.counts || {};
    const storage = body.storage || {};
    const warning = body.cleanup_warning ? `<div class="note-box danger">${escapeHtml(body.cleanup_warning)}</div>` : "";
    showModal("导入完成", `<p>新版本 ${escapeHtml(body.snapshot_id)} 已成功发布。当前激活版本${body.current_snapshot_id === body.snapshot_id ? "已设为此版本" : "保持不变"}。</p>${fileRows}<div class="note-box">差异：新增 ${counts.added ?? 0} · 继承 ${counts.inherited ?? 0} · 替换 ${counts.replaced ?? 0} · 移除 ${counts.removed ?? 0}<br>对象：新增 ${storage.new_objects ?? 0} · 复用 ${storage.reused_objects ?? 0}<br>字节：实际新增 ${storage.new_object_bytes ?? 0} · 复用 ${storage.reused_object_bytes ?? 0}</div>${warning}`, '<button class="primary" type="button" data-action="close-modal">查看新版本</button>');
    state.buildFiles = [];
  } catch (error) {
    if (libraryId !== state.libraryId || libraryEpoch !== state.libraryEpoch) return;
    const returned = Array.isArray(error.payload?.files) ? error.payload.files : files.map(file => ({name: file.name, stage: "失败、未发布", error: error.message}));
    $("#selected-files").innerHTML = returned.map(item => `<div><strong>${escapeHtml(item.name)}</strong> · ${escapeHtml(item.stage)}<br><small class="danger">${escapeHtml(item.error || "")}</small></div>`).join("");
    $("#build-error").textContent = `${error.message} 没有产生残缺成功版本。`;
    button.disabled = false;
    render();
  } finally {
    if (requestId === state.requests.build) {
      state.building = false;
      render();
    }
  }
}

function validMcpSelfCheck(report) {
  if (!report || typeof report !== "object" || Array.isArray(report)) return false;
  if (
    report.scope !== "local_stdio_self_check"
    || report.evidence_level !== "offline_local_stdio"
    || typeof report.observation_scope_zh !== "string" || !report.observation_scope_zh.trim()
    || typeof report.message !== "string" || !report.message.trim()
    || typeof report.passed !== "boolean"
    || report.network_calls !== null
    || report.external_config_writes !== null
    || report.model_calls !== 0
    || report.api_keys_used !== 0
    || report.retry_count !== 0
    || !Array.isArray(report.steps) || !report.steps.length
  ) return false;
  if (!report.steps.every(step => step && typeof step === "object" && !Array.isArray(step)
    && typeof step.name === "string" && step.name.trim()
    && typeof step.message === "string" && step.message.trim()
    && typeof step.passed === "boolean")) return false;
  if (report.passed) return report.tool_count === 8 && report.steps.length === 6 && report.steps.every(step => step.passed);
  return report.tool_count === null || report.tool_count === 8;
}

function validSimulatedTunnelReport(report, action) {
  if (!report || typeof report !== "object" || Array.isArray(report)) return false;
  const running = report.state === "模拟运行" || report.state === "模拟通过";
  const unknown = report.state === "模拟状态未知";
  return report.mode === "offline_simulation"
    && ["status", "start", "health", "stop"].includes(report.action)
    && (!action || report.action === action)
    && ["模拟停止", "模拟运行", "模拟通过", "模拟状态未知"].includes(report.state)
    && typeof report.passed === "boolean"
    && (!unknown || report.passed === false)
    && report.simulated === true
    && report.simulated_running === (unknown ? null : running)
    && report.real_connected === false
    && report.local_health_checked === (report.state === "模拟通过")
    && report.chatgpt_tool_discovery_checked === false
    && ["network_calls", "model_calls", "api_keys_collected", "external_config_writes", "retry_count"].every(key => report[key] === 0)
    && typeof report.message === "string" && report.message.trim()
    && (report.error_code === undefined || typeof report.error_code === "string")
    && (!report.passed || report.action === "status" || report.state === {start: "模拟运行", health: "模拟通过", stop: "模拟停止"}[report.action]);
}

document.addEventListener("click", async event => {
  const button = event.target.closest?.("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  const id = button.dataset.id || "";
  if (action === "context-details" || action === "context-remove") documentMenuAction(action);
  else if (action === "nav") {
    state.page = button.dataset.page;
    if (narrowLayout() && state.page === "search") state.detailsOpen = false;
    render();
  }
  else if (["settings", "apps", "help"].includes(action)) { state.page = action; render(); }
  else if (action === "toggle-details") { state.detailsOpen = !state.detailsOpen; renderDetails(); }
  else if (action === "select-document") loadDocumentDetail(id, {chunkId: button.dataset.chunkId || ""});
  else if (action === "read-section") loadSection(id);
  else if (action === "clear-filter") { state.removalIds = []; state.filter = ""; state.mediaFilter = "all"; render(); }
  else if (action === "manage-documents") { state.managing = !state.managing; state.removalIds = []; render(); }
  else if (action === "select-filtered") { state.removalIds = filteredDocuments().map(item => item.document_id); render(); }
  else if (action === "clear-selection") { state.removalIds = []; render(); }
  else if (action === "remove-documents") removalModal();
  else if (action === "confirm-remove") await removeDocuments();
  else if (action === "view-removed-version") { $("#modal").close(); state.page = "documents"; browseSnapshot(id); }
  else if (action === "new-library") libraryModal();
  else if (action === "manage-libraries") manageLibrariesModal();
  else if (action === "edit-library" || action === "library-settings") libraryModal(id || state.libraryId);
  else if (action === "delete-library-dialog") await deleteLibraryDialog(id);
  else if (action === "confirm-delete-library") await deleteLibrary();
  else if (action === "save-library") await saveLibrary(id);
  else if (action === "close-modal") {
    if (state.building || state.deleting) toast("操作正在进行，请等待结果");
    else $("#modal").close();
  }
  else if (action === "import") { state.buildFiles = []; importModal(); }
  else if (action === "start-import") await startImport();
  else if (action === "browse-version") browseSnapshot(id);
  else if (action === "return-active") browseSnapshot(state.currentSnapshotId);
  else if (action === "copy-snapshot-id") await copySnapshotId(id);
  else if (action === "verify-version") await verifySnapshot(id);
  else if (action === "activate-version") await activateSnapshot(id);
  else if (action === "vector-version") globalThis.FolioConnections?.showVectorDialog(id);
  else if (globalThis.FolioConnections?.handleAction) {
    await globalThis.FolioConnections.handleAction(action, id, button);
  }
});

document.addEventListener("change", event => {
  if (event.target.dataset?.removalId) {
    const id = event.target.dataset.removalId;
    if (state.managing && filteredDocuments().some(item => item.document_id === id)) {
      state.removalIds = state.removalIds.filter(item => item !== id);
      if (event.target.checked) state.removalIds.push(id);
      render();
      document.querySelector(`[data-removal-id="${CSS.escape(id)}"]`)?.focus();
    }
  }
  else if (event.target.id === "library-select") selectLibrary(event.target.value);
  else if (event.target.id === "kind-filter") { state.removalIds = []; state.mediaFilter = event.target.value; render(); }
  else if (event.target.id === "search-mode") {
    globalThis.FolioConnections?.setEnhancedEnabled(event.target.value === "enhanced");
    invalidateSearch($("#search-input")?.value ?? state.search.query);
  }
  else if (event.target.id === "source-files") {
    const files = Array.from(event.target.files || []);
    state.buildFiles = files;
    $("#selected-files").textContent = files.length ? files.map(file => `${file.name} · 待提交`).join("\n") : "尚未选择文件。";
  } else globalThis.FolioConnections?.handleChange?.(event);
});

document.addEventListener("input", event => {
  if (event.target.id === "document-filter") {
    state.removalIds = [];
    state.filter = event.target.value;
    const position = event.target.selectionStart;
    render();
    const input = $("#document-filter");
    input?.focus();
    input?.setSelectionRange?.(position, position);
  } else if (event.target.id === "delete-library-name") {
    const button = document.querySelector('[data-action="confirm-delete-library"]');
    if (button) button.disabled = state.deleting || event.target.value !== state.deletionTarget?.name;
  } else if (event.target.id === "search-input") {
    state.search.query = event.target.value;
  } else globalThis.FolioConnections?.handleInput?.(event);
});

document.addEventListener("submit", event => {
  if (event.target.id === "search-form") {
    event.preventDefault();
    runSearch($("#search-input").value);
  }
});

document.addEventListener("dragover", event => {
  if (event.dataTransfer?.types?.includes("Files")) event.preventDefault();
});

document.addEventListener("drop", event => {
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  state.buildFiles = Array.from(event.dataTransfer.files);
  importModal();
});

document.addEventListener("contextmenu", event => {
  const row = event.target.closest?.(".doc-row");
  if (!row) { closeDocumentMenu(); return; }
  event.preventDefault();
  openDocumentMenu(row, event.clientX, event.clientY);
});

document.addEventListener("pointerdown", event => {
  if (documentMenuTarget && !$("#document-menu").contains(event.target)) closeDocumentMenu();
});
document.addEventListener("scroll", event => {
  if (documentMenuTarget && !$("#document-menu").contains(event.target)) closeDocumentMenu();
}, true);
window.addEventListener?.("resize", () => closeDocumentMenu());

document.addEventListener("keydown", event => {
  const row = event.target.closest?.(".doc-row");
  if (row && (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) {
    event.preventDefault();
    const bounds = row.getBoundingClientRect();
    openDocumentMenu(row, bounds.left + 16, bounds.bottom);
    return;
  }
  if (documentMenuTarget) {
    if (event.key === "Escape" || event.key === "Tab") {
      if (event.key === "Escape") event.preventDefault();
      closeDocumentMenu(true);
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const items = Array.from($("#document-menu").querySelectorAll('[role="menuitem"]')).filter(item => !item.disabled);
      const index = items.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
      return;
    }
  }
  if (event.key === "Escape" && !$("#modal").open && state.detailsOpen && narrowLayout()) {
    state.detailsOpen = false;
    renderDetails();
  }
});

$("#modal").addEventListener("cancel", event => {
  if (state.building || state.deleting) {
    event.preventDefault();
    toast("操作正在进行，请等待结果");
  }
});

globalThis.FolioApp = {
  state,
  render,
  renderDetails,
  requestJson,
  writeOptions,
  loadStatus,
  loadLibraries,
  selectLibrary,
  loadSnapshots,
  loadDocuments,
  browseSnapshot,
  verifySnapshot,
  activateSnapshot,
  loadDocumentDetail,
  loadSection,
  runSearch,
  invalidateSearch,
  selectionCapture,
  selectionIsCurrent,
  validMcpSelfCheck,
  validSimulatedTunnelReport,
  toast,
  showModal,
  escapeHtml,
  chip,
  heading,
  empty,
  shortSnapshot,
};

document.addEventListener("DOMContentLoaded", async () => {
  render();
  await loadStatus();
  await loadLibraries();
});
