"use strict";

let vectorPlan = null;
let realTunnelRequest = 0;
let realTunnelBusy = false;
const connectionElement = (id) => document.getElementById(id);

function renderRealTunnel(report) {
  const names = {stopped: "已停止", starting: "正在连接", connected: "本地连接已验证",
    error: "连接失败", unknown: "状态未知"};
  connectionElement("real-tunnel-status").textContent = report
    ? `${names[report.state] || report.state}；ChatGPT 工具发现：尚未验证。${report.error_code || ""}`
    : "真实连接尚未准备。";
}

function renderConnections(settings, updateTunnel = true) {
  connectionElement("real-connections").hidden = !settings;
  if (!settings) return;
  connectionElement("aliyun-key-state").textContent = settings.aliyun_configured
    ? "已保存到本机钥匙串；实际模型调用尚需验证。" : "尚未填写。";
  connectionElement("openai-key-state").textContent = settings.openai_tunnel_configured
    ? "已保存到本机钥匙串；实际 Tunnel 认证尚需验证。" : "尚未填写。";
  connectionElement("real-tunnel-id").value = settings.tunnel_id;
  connectionElement("accept-tunnel-backoff").checked = settings.tunnel_backoff_accepted;
  if (updateTunnel) renderRealTunnel(settings.tunnel);
}

async function saveConnectionKey(kind, field, button) {
  const input = connectionElement(field);
  let key = input.value;
  input.value = "";
  connectionElement(button).disabled = true;
  try {
    if (!key.trim()) throw new Error("请先填写 Key。");
    const request = api("/api/connections/credentials", {method: "POST",
      headers: actionHeaders("save-credential", true), body: JSON.stringify({kind, key})});
    key = "";
    await request;
    connectionElement("connection-notice").textContent = "已保存到本机钥匙串；没有联网测试。";
    await loadStatus();
  } catch (error) {
    connectionElement("connection-notice").textContent = error.message;
  } finally {
    key = "";
    connectionElement(button).disabled = false;
  }
}

function clearVectorPlan() {
  vectorPlan = null;
  connectionElement("build-real-vectors").disabled = true;
  connectionElement("real-vector-plan").textContent = "";
}

async function realVectors(action) {
  const libraryId = state.libraryId;
  const snapshotId = elements.snapshotSelect.value;
  if (!libraryId || !snapshotId) {
    connectionElement("real-vector-plan").textContent = "请先选择资料库和已核验快照。";
    return;
  }
  if (action === "build" && (!vectorPlan || vectorPlan.libraryId !== libraryId || vectorPlan.snapshotId !== snapshotId)) return;
  connectionElement("build-real-vectors").disabled = true;
  connectionElement("preview-real-vectors").disabled = true;
  try {
    const payload = await api(`/api/libraries/${encodeURIComponent(libraryId)}/vectors/${action}`, {
      method: "POST", headers: actionHeaders(`vectors-${action}`, true),
      body: JSON.stringify({snapshot_id: snapshotId}),
    });
    if (libraryId !== state.libraryId || snapshotId !== elements.snapshotSelect.value) return;
    const value = payload.vectors;
    if (action === "preview") {
      vectorPlan = {libraryId, snapshotId};
      connectionElement("real-vector-plan").textContent =
        `预计新增 ${value.new_inputs} 块、${value.new_input_chars} 字符，发送到阿里云；最多 ${value.planned_calls} 次模型请求，复用 ${value.reused_inputs} 块。可能产生按量费用。`;
      connectionElement("build-real-vectors").disabled = false;
    } else {
      vectorPlan = null;
      connectionElement("real-vector-plan").textContent =
        `向量已就绪，本次模型请求 ${value.provider_audit ? value.provider_audit.call_count : 0} 次。可在上方明确选择增强搜索。`;
    }
  } catch (error) {
    vectorPlan = null;
    const audit = error.payload && error.payload.provider_audit;
    const sent = audit ? ` 本次已发送 ${audit.call_count} 次模型请求，已发送的请求可能计费。` : "";
    const usage = audit && audit.calls.filter((call) => call.usage).map((call) =>
      `${call.model_id}：${JSON.stringify(call.usage)}`).join("；");
    connectionElement("real-vector-plan").textContent =
      `${error.message} 已停止，没有自动重试。${sent}${usage ? ` 服务商返回用量：${usage}` : ""}`;
  } finally {
    connectionElement("preview-real-vectors").disabled = false;
  }
}

async function saveRealTunnel() {
  try {
    const result = await api("/api/connections/tunnel", {method: "POST",
      headers: actionHeaders("save-tunnel-settings", true), body: JSON.stringify({
        tunnel_id: connectionElement("real-tunnel-id").value.trim(),
        accept_backoff: connectionElement("accept-tunnel-backoff").checked,
      })});
    renderConnections(result.connections, false);
    connectionElement("connection-notice").textContent = "连接设置已保存；尚未启动 Tunnel。";
  } catch (error) {
    connectionElement("connection-notice").textContent = error.message;
  }
}

async function runRealTunnel(action) {
  const requestId = ++realTunnelRequest;
  realTunnelBusy = true;
  connectionElement("real-tunnel-start").disabled = true;
  try {
    const path = action === "start" ? "/api/tunnel/production-start" : `/api/tunnel/production/${action}`;
    const payload = await api(path, {method: "POST", headers: actionHeaders(`tunnel-production-${action}`)});
    if (requestId === realTunnelRequest) renderRealTunnel(payload.tunnel);
  } catch (error) {
    if (requestId !== realTunnelRequest) return;
    if (error.payload && error.payload.tunnel) renderRealTunnel(error.payload.tunnel);
    else connectionElement("real-tunnel-status").textContent = `${error.message} 未确认连接。`;
  } finally {
    if (requestId === realTunnelRequest) {
      realTunnelBusy = false;
      connectionElement("real-tunnel-start").disabled = false;
    }
  }
}

connectionElement("save-aliyun-secret").addEventListener("click", () => saveConnectionKey("aliyun", "aliyun-secret", "save-aliyun-secret"));
connectionElement("save-openai-secret").addEventListener("click", () => saveConnectionKey("openai_tunnel", "openai-secret", "save-openai-secret"));
connectionElement("save-real-tunnel").addEventListener("click", saveRealTunnel);
connectionElement("preview-real-vectors").addEventListener("click", () => realVectors("preview"));
connectionElement("build-real-vectors").addEventListener("click", () => realVectors("build"));
elements.snapshotSelect.addEventListener("change", clearVectorPlan);
elements.librarySelect.addEventListener("change", clearVectorPlan);
for (const action of ["start", "health", "stop"]) {
  connectionElement(`real-tunnel-${action}`).addEventListener("click", () => runRealTunnel(action));
}

renderConnections(state.connections);
