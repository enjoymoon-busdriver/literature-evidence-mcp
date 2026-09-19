"use strict";

(() => {
  const app = globalThis.FolioApp;
  if (!app) return;
  const roles = [
    ["query_rewrite", "搜索词优化", "换个说法找资料"],
    ["vector_recall", "向量模型", "找意思相近的内容；当前向量维度固定为 1024"],
    ["candidate_rerank", "排序模型", "让更相关的候选排在前面"],
  ];
  const local = {
    connections: null,
    modelDraft: null,
    connectionCheck: null,
    settingsBusy: false,
    credentialBusy: false,
    modelCheckRequestId: 0,
    mcpRequestId: 0,
    tunnelRequestId: 0,
    tunnelAction: "",
    tunnelTimer: null,
    tunnelCheckUntil: 0,
    tunnelRevision: 0,
    simulatedRequestId: 0,
    simulatedAction: "",
    simulatedRevision: 0,
    vectorRequestId: 0,
    vectorTarget: null,
    vectorPreviewReady: false,
  };

  const defaultModels = {
    query_rewrite: "qwen3.8-flash",
    vector_recall: "qwen3.7-text-embedding",
    candidate_rerank: "qwen3-rerank",
  };

  function settings() {
    return local.connections?.model_settings || {
      enabled: false,
      provider: "aliyun-beijing",
      region: "cn-beijing",
      model_ids: defaultModels,
      recommended_model_ids: defaultModels,
    };
  }

  function revisions() {
    return {
      tunnel: local.tunnelRevision,
      simulated: local.simulatedRevision,
      tunnelBusy: Boolean(local.tunnelAction),
      simulatedBusy: Boolean(local.simulatedAction),
    };
  }

  function invalidateVectorPlan() {
    local.vectorRequestId += 1;
    local.vectorPreviewReady = false;
    const plan = document.querySelector?.("#vector-plan");
    const build = document.querySelector?.('[data-action="build-vectors"]');
    const preview = document.querySelector?.('[data-action="preview-vectors"]');
    if (plan && local.vectorTarget?.lastPlan) plan.textContent = local.vectorTarget.lastPlan;
    if (build) build.disabled = true;
    if (preview) preview.disabled = false;
  }

  function acceptStatus(body, capturedRevisions = null) {
    if (body.connections) {
      const previousModels = JSON.stringify(settings().model_ids);
      const keepTunnel = capturedRevisions
        && (capturedRevisions.tunnel !== local.tunnelRevision || capturedRevisions.tunnelBusy);
      local.connections = keepTunnel && local.connections
        ? {...body.connections, tunnel: local.connections.tunnel}
        : body.connections;
      local.modelDraft = {...settings().model_ids};
      if (previousModels !== JSON.stringify(settings().model_ids)) invalidateVectorPlan();
    }
    if (
      !local.simulatedAction
      && (!capturedRevisions || (
        capturedRevisions.simulated === local.simulatedRevision
        && !capturedRevisions.simulatedBusy
      ))
      && body.tunnel_wizard?.simulation
    ) {
      app.state.simulatedTunnel = body.tunnel_wizard.simulation;
    }
    app.render();
  }

  function enhancedAvailable() {
    return Boolean(local.connections && settings().enabled && local.connections.aliyun_configured);
  }

  function enhancedEnabled() {
    return app.state.searchMode === "enhanced" && enhancedAvailable();
  }

  function setEnhancedEnabled(enabled) {
    app.state.searchMode = enabled && enhancedAvailable() ? "enhanced" : "bm25";
  }

  function checkRows() {
    const items = local.connectionCheck?.results;
    if (!Array.isArray(items)) {
      return roles.map(([, name]) => `<div class="connection-result"><strong>${name}</strong> · 尚未检查</div>`).join("");
    }
    return items.map(item => {
      const tone = item.passed ? "green" : "red";
      return `<div class="connection-result"><strong>${app.escapeHtml(roles.find(role => role[0] === item.role)?.[1] || item.role)}</strong> ${app.chip(item.passed ? "可用" : "不可用", tone)}<br><span class="muted">${app.escapeHtml(item.model_id)} · ${app.escapeHtml(item.message)}</span></div>`;
    }).join("");
  }

  function renderSettings() {
    const value = settings();
    const configured = Boolean(local.connections?.aliyun_configured);
    const draft = local.modelDraft || value.model_ids;
    return app.heading("全局设置", "配置可选的 AI 增强搜索。默认关闭，本地 BM25 始终可用。", '<button type="button" data-action="open-apps">连接 AI 应用 →</button>') + `
      <section class="settings-card">
        <h2>AI 增强搜索</h2>
        <label class="enhancement-toggle"><input id="enhanced-search" type="checkbox" ${value.enabled ? "checked" : ""} ${local.settingsBusy ? "disabled" : ""}> 启用 AI 增强搜索</label>
        <p>关闭时，搜索只在本机执行 BM25。开启后，仅在明确选择“AI 增强搜索”时调用模型。</p>
        <div class="field"><strong>服务商</strong><p>阿里云百炼 · 北京（cn-beijing）</p></div>
        <div class="field"><strong>共享 API Key</strong><p>${configured ? "•••• · 已保存在本机钥匙串" : "未配置"}</p><small>搜索词优化、向量和排序共用这一份 Key。保存不会联网检查，也不会自动开启增强。</small></div>
        <label class="field">阿里云 API Key<input id="aliyun-secret" type="password" maxlength="512" autocomplete="off" placeholder="${configured ? "输入新 Key 可替换" : "仅发送到本机"}" ${local.credentialBusy ? "disabled" : ""}></label>
        <div class="setting-actions"><button type="button" data-action="save-aliyun-key" ${local.credentialBusy ? "disabled" : ""}>保存到本机钥匙串</button><button class="primary" type="button" data-action="recommend-models" ${local.settingsBusy ? "disabled" : ""}>使用推荐配置</button></div>
        <p style="margin-top:12px">推荐配置会恢复三个型号并保留 Key；若向量型号改变，需为对应版本重新准备向量，保存设置不会自动重建。</p>
      </section>
      <section class="settings-card">
        <h2>检查连接</h2>
        <p>只在你点击后，三个角色各检查一次并发送少量测试内容；可能产生费用，失败不会自动重试。</p>
        <button type="button" data-action="check-models" ${local.settingsBusy ? "disabled" : ""}>${local.settingsBusy ? "正在处理…" : "检查三个模型连接"}</button>
        <div id="connection-result" role="status" aria-live="polite">${checkRows()}</div>
      </section>
      <section class="settings-card">
        <details id="advanced-settings">
          <summary>高级设置 · 三个模型角色</summary>
          <p>修改向量模型后，已有版本的向量可能需要重新准备；保存设置不会自动重建。</p>
          ${roles.map(([role, name, description]) => `<label class="field">${name}<small>${description}</small><input data-model-role="${role}" maxlength="100" value="${app.escapeHtml(draft[role] || "")}" ${local.settingsBusy ? "disabled" : ""}></label>`).join("")}
          <button type="button" data-action="save-models" ${local.settingsBusy ? "disabled" : ""}>保存模型设置</button>
        </details>
      </section>`;
  }

  function tunnelStateLabel(report) {
    const stateValue = report?.state || "unknown";
    const labels = {
      connected: "已连接（本地 Tunnel）",
      starting: "启动中",
      not_ready: "未就绪",
      error: "连接失败",
      unknown: "状态未知",
      running: "运行中",
      stopped: "已停止",
      failed: "连接失败",
      "模拟运行": "模拟运行",
      "模拟停止": "模拟停止",
      "模拟通过": "模拟健康检查通过",
      "模拟状态未知": "模拟状态未知",
    };
    return labels[stateValue] || stateValue;
  }

  function renderApps() {
    const guide = app.state.mcpGuide || {};
    const copyReady = guide.state === "copy_ready_not_configured";
    const connections = local.connections || {};
    const simulation = app.state.simulatedTunnel;
    const mcp = app.state.mcpSelfCheck;
    return app.heading("连接 AI 应用", "本地 MCP 无需 OpenAI Key；真实 Tunnel 单独配置运行 Key。", '<button type="button" data-action="open-settings">返回全局设置</button>') + `
      <section class="settings-card">
        <h2>本机 MCP · 八个只读工具</h2>
        <p>适用于同一台电脑上的 ChatGPT desktop、Codex CLI 与 Codex IDE 扩展。复制不会执行命令或写配置。</p>
        <div class="two-column">
          <label class="field">可复制 CLI<textarea id="mcp-cli" class="code-copy" rows="4" readonly>${app.escapeHtml(copyReady ? guide.cli : "")}</textarea><button type="button" data-action="copy-mcp-cli" ${copyReady ? "" : "disabled"}>复制 CLI（不执行）</button></label>
          <label class="field">等价 TOML<textarea id="mcp-toml" class="code-copy" rows="4" readonly>${app.escapeHtml(copyReady ? guide.toml : "")}</textarea><button type="button" data-action="copy-mcp-toml" ${copyReady ? "" : "disabled"}>复制 TOML（不写配置）</button></label>
        </div>
        ${copyReady ? "" : `<div class="note-box danger">${app.escapeHtml(guide.reason || "本地 MCP 启动入口尚未准备。")}</div>`}
        <button type="button" data-action="mcp-self-check" ${!copyReady || local.mcpRequestId ? "disabled" : ""}>${local.mcpRequestId ? "正在运行自检…" : "运行本地离线 STDIO 自检"}</button>
        <div class="note-box" role="status">${mcp ? (mcp.passed ? `自检通过：${mcp.tool_count} 个只读工具；零模型、零 Key、零重试。仍需在客户端手工配置。` : app.escapeHtml(mcp.message || "自检未通过")) : "尚未运行自检。通过不代表客户端已配置。"}</div>
      </section>
      <section class="settings-card">
        <h2>ChatGPT Secure MCP Tunnel · 真实连接</h2>
        <p>运行 Key 只保存在本机钥匙串。启动后通过出站 HTTPS 连接；本地健康不等于 ChatGPT 已发现工具。</p>
        <p>连接程序：${connections.tunnel?.client_installed ? "已找到（应用自带或 Homebrew 安装）" : "未找到；请按教程通过 Homebrew 安装 tunnel-client，再刷新页面"}。检测程序不会读取 Key 或启动连接。</p>
        <details><summary>真实连接所需权限</summary><p>OpenAI Platform 组织需关联正确的 ChatGPT 工作区。创建 Tunnel 需要 Read + Manage；选择或运行已有 Tunnel，以及 ChatGPT developer mode 中的创建者，需要该 Tunnel 的 Read + Use。</p></details>
        <label class="field">OpenAI Tunnel 运行 Key<input id="openai-secret" type="password" maxlength="512" autocomplete="off" placeholder="${connections.openai_tunnel_configured ? "已配置；输入新 Key 可替换" : "仅发送到本机"}"></label>
        <button type="button" data-action="save-openai-key">保存运行 Key 到本机钥匙串</button>
        <label class="field">本项目 Tunnel ID<input id="real-tunnel-id" maxlength="135" autocomplete="off" value="${app.escapeHtml(connections.tunnel_id || "")}"></label>
        <label class="enhancement-toggle"><input id="accept-tunnel-backoff" type="checkbox" ${connections.tunnel_backoff_accepted ? "checked" : ""}> 接受官方 Tunnel 客户端断线退避重连；应用不自动重启进程</label>
        <button type="button" data-action="save-real-tunnel">保存连接设置</button>
        <div class="tunnel-actions" style="margin-top:18px"><button class="primary" type="button" data-action="real-tunnel-start" ${local.tunnelAction ? "disabled" : ""}>启动真实 Tunnel</button><button type="button" data-action="real-tunnel-health" ${local.tunnelAction ? "disabled" : ""}>检查真实连接</button><button type="button" data-action="real-tunnel-stop" ${["start", "stop"].includes(local.tunnelAction) ? "disabled" : ""}>停止真实 Tunnel</button></div>
        <div id="real-tunnel-status" class="note-box" role="status">${app.escapeHtml(realTunnelStatusText())}</div>
      </section>
      <section class="settings-card">
        <h2>ChatGPT Secure MCP Tunnel（离线模拟）</h2>
        <p>独立的流程演练：不收集真实 Key，不建立连接，网络调用与外部配置写入均为 0。</p>
        <div class="tunnel-actions"><button type="button" data-action="simulated-start" ${local.simulatedAction ? "disabled" : ""}>模拟启动</button><button type="button" data-action="simulated-health" ${local.simulatedAction ? "disabled" : ""}>检查模拟健康状态</button><button type="button" data-action="simulated-stop" ${["start", "stop"].includes(local.simulatedAction) ? "disabled" : ""}>模拟停止</button></div>
        <div class="note-box" role="status">离线模拟：${app.escapeHtml(tunnelStateLabel(simulation))}${simulation?.message ? ` · ${app.escapeHtml(simulation.message)}` : ""}</div>
      </section>`;
  }

  function acceptConnections(body) {
    if (body.connections) {
      local.connections = body.connections;
      local.modelDraft = {...settings().model_ids};
      app.state.status = {...(app.state.status || {}), connections: body.connections};
    }
    app.render();
  }

  async function saveCredential(kind, fieldId) {
    const field = document.querySelector(`#${fieldId}`);
    const key = field?.value || "";
    if (!key.trim()) return app.toast("请先填写 Key");
    field.value = "";
    local.modelCheckRequestId += 1;
    local.connectionCheck = null;
    local.credentialBusy = true;
    app.render();
    try {
      const body = await app.requestJson("/api/connections/credentials", app.writeOptions("save-credential", {kind, key}));
      acceptConnections(body);
      app.toast("Key 已保存到本机钥匙串；未联网检查");
    } catch (error) {
      app.toast(error.message);
    } finally {
      local.credentialBusy = false;
      app.render();
    }
  }

  async function saveModels(enabled = settings().enabled) {
    local.modelCheckRequestId += 1;
    app.invalidateSearch?.();
    invalidateVectorPlan();
    local.settingsBusy = true;
    app.render();
    try {
      const body = await app.requestJson("/api/connections/models", app.writeOptions("save-model-settings", {
        enabled,
        model_ids: {...(local.modelDraft || settings().model_ids)},
      }));
      local.connectionCheck = null;
      acceptConnections(body);
      await app.loadSnapshots();
      app.toast(enabled ? "AI 增强搜索已启用" : "AI 增强搜索已关闭");
    } catch (error) {
      app.toast(error.message);
    } finally {
      local.settingsBusy = false;
      app.render();
    }
  }

  async function restoreRecommended() {
    local.modelCheckRequestId += 1;
    app.invalidateSearch?.();
    invalidateVectorPlan();
    local.settingsBusy = true;
    app.render();
    try {
      const body = await app.requestJson("/api/connections/models/recommended", app.writeOptions("restore-recommended-models"));
      local.connectionCheck = null;
      acceptConnections(body);
      await app.loadSnapshots();
      app.toast("已恢复推荐型号；共享 Key 与启用状态保持不变");
    } catch (error) {
      app.toast(error.message);
    } finally {
      local.settingsBusy = false;
      app.render();
    }
  }

  async function checkModels() {
    const requestId = ++local.modelCheckRequestId;
    local.settingsBusy = true;
    local.connectionCheck = null;
    app.render();
    try {
      const body = await app.requestJson("/api/connections/models/check", app.writeOptions("check-model-connections"));
      if (requestId !== local.modelCheckRequestId) return;
      local.connectionCheck = body.connection_check || null;
    } catch (error) {
      if (requestId !== local.modelCheckRequestId) return;
      local.connectionCheck = {passed: false, results: roles.map(([role]) => ({
        role, model_id: settings().model_ids[role], passed: false, message: error.message,
      }))};
    } finally {
      if (requestId === local.modelCheckRequestId) {
        local.settingsBusy = false;
        app.render();
      }
    }
  }

  async function runMcpSelfCheck() {
    const requestId = ++local.mcpRequestId;
    app.state.mcpSelfCheck = null;
    app.render();
    try {
      const body = await app.requestJson("/api/mcp-self-check", app.writeOptions("mcp-self-check"));
      if (requestId !== local.mcpRequestId) return;
      if (!app.validMcpSelfCheck(body.self_check)) throw new Error("本地自检返回的数据格式无效。");
      app.state.mcpSelfCheck = body.self_check;
    } catch (error) {
      if (requestId === local.mcpRequestId) app.state.mcpSelfCheck = {passed: false, message: error.message};
    } finally {
      if (requestId === local.mcpRequestId) local.mcpRequestId = 0;
      app.render();
    }
  }

  async function saveTunnelSettings() {
    const tunnelId = document.querySelector("#real-tunnel-id")?.value.trim() || "";
    const acceptBackoff = Boolean(document.querySelector("#accept-tunnel-backoff")?.checked);
    try {
      const body = await app.requestJson("/api/connections/tunnel", app.writeOptions("save-tunnel-settings", {tunnel_id: tunnelId, accept_backoff: acceptBackoff}));
      acceptConnections(body);
      app.toast("真实 Tunnel 设置已保存；尚未启动");
    } catch (error) {
      app.toast(error.message);
    }
  }

  function realTunnelStatusText() {
    const tunnel = local.connections?.tunnel || {};
    const pending = {start: "启动中…", health: "检查中…", stop: "正在停止…"}[local.tunnelAction];
    return `真实 Tunnel：${pending || tunnelStateLabel(tunnel)}${!pending && tunnel.message ? ` · ${tunnel.message}` : ""}`;
  }

  function renderRealTunnelStatus() {
    const status = document.querySelector("#real-tunnel-status");
    if (status) status.textContent = realTunnelStatusText();
  }

  async function runRealTunnel(action, automatic = false) {
    clearTimeout(local.tunnelTimer);
    local.tunnelTimer = null;
    if (action === "start") local.tunnelCheckUntil = Date.now() + 45000;
    if (action === "stop") local.tunnelCheckUntil = 0;
    local.tunnelRevision += 1;
    const requestId = ++local.tunnelRequestId;
    local.tunnelAction = action;
    if (automatic) renderRealTunnelStatus();
    else app.render();
    const path = action === "start" ? "/api/tunnel/production-start" : `/api/tunnel/production/${action}`;
    const intent = `tunnel-production-${action}`;
    try {
      const body = await app.requestJson(path, app.writeOptions(intent));
      if (requestId !== local.tunnelRequestId) return;
      local.connections = {...(local.connections || {}), tunnel: body.tunnel};
    } catch (error) {
      if (requestId === local.tunnelRequestId) {
        const report = error.payload?.tunnel;
        local.connections = {...(local.connections || {}), tunnel: report || {state: "failed", passed: false, message: error.message}};
      }
    } finally {
      if (requestId === local.tunnelRequestId) {
        local.tunnelAction = "";
        const tunnel = local.connections?.tunnel;
        if (action !== "stop" && tunnel?.running === true
            && (tunnel.state === "starting"
              || (tunnel.state === "not_ready" && Date.now() < local.tunnelCheckUntil))) {
          local.tunnelTimer = setTimeout(() => {
            if (requestId === local.tunnelRequestId) runRealTunnel("health", true);
          }, action === "start" ? 0 : 2000);
        }
      }
      if (automatic) renderRealTunnelStatus();
      else app.render();
    }
  }

  async function runSimulatedTunnel(action) {
    local.simulatedRevision += 1;
    const requestId = ++local.simulatedRequestId;
    local.simulatedAction = action;
    app.render();
    try {
      const body = await app.requestJson(`/api/tunnel/simulated/${action}`, app.writeOptions(`tunnel-simulated-${action}`));
      if (requestId !== local.simulatedRequestId) return;
      if (!app.validSimulatedTunnelReport(body.tunnel, action)) throw new Error("离线模拟返回的数据格式无效。");
      app.state.simulatedTunnel = body.tunnel;
    } catch (error) {
      if (requestId === local.simulatedRequestId) app.toast(error.message);
    } finally {
      if (requestId === local.simulatedRequestId) local.simulatedAction = "";
      app.render();
    }
  }

  function vectorCurrent(capture) {
    return capture.requestId === local.vectorRequestId
      && capture.libraryId === app.state.libraryId
      && capture.libraryEpoch === app.state.libraryEpoch
      && local.vectorTarget?.libraryId === capture.libraryId
      && local.vectorTarget?.snapshotId === capture.snapshotId;
  }

  function showVectorDialog(snapshotId) {
    const snapshot = app.state.snapshots.find(item => item.snapshot_id === snapshotId);
    if (!snapshot) return;
    local.vectorRequestId += 1;
    local.vectorTarget = {
      libraryId: app.state.libraryId,
      libraryEpoch: app.state.libraryEpoch,
      snapshotId,
      modelIds: JSON.stringify(settings().model_ids),
    };
    local.vectorPreviewReady = false;
    app.showModal(
      "准备版本向量",
      `<p>文献库：${app.escapeHtml(app.state.libraries.find(item => item.library_id === app.state.libraryId)?.name || "")}<br>版本：${app.escapeHtml(snapshotId)} · ${snapshot.members?.length || 0} 篇文档</p><div id="vector-plan" class="note-box">先查看预计发送量和调用数，再明确把尚未复用的文本块发送到阿里云北京地域；模型调用可能按量计费。</div>`,
      '<button type="button" data-action="close-modal">关闭</button><button type="button" data-action="preview-vectors">查看发送量</button><button class="primary" type="button" data-action="build-vectors" disabled>明确发送并构建向量</button>',
    );
  }

  async function vectors(action) {
    if (!local.vectorTarget || (action === "build" && !local.vectorPreviewReady)) return;
    const requestId = ++local.vectorRequestId;
    const capture = {...local.vectorTarget, requestId};
    const plan = document.querySelector("#vector-plan");
    const previewButton = document.querySelector('[data-action="preview-vectors"]');
    const buildButton = document.querySelector('[data-action="build-vectors"]');
    local.vectorPreviewReady = false;
    if (plan) local.vectorTarget.lastPlan = plan.textContent;
    if (previewButton) previewButton.disabled = true;
    if (buildButton) buildButton.disabled = true;
    if (plan) plan.textContent = action === "preview" ? "正在计算本次发送量…" : "正在构建向量；模型请求零自动重试…";
    if (action === "build") {
      const snapshot = app.state.snapshots.find(item => item.snapshot_id === capture.snapshotId);
      if (snapshot) snapshot.vector_status = "preparing";
      app.render();
    }
    try {
      const body = await app.requestJson(
        `/api/libraries/${encodeURIComponent(capture.libraryId)}/vectors/${action}`,
        app.writeOptions(`vectors-${action}`, {snapshot_id: capture.snapshotId}),
      );
      if (!vectorCurrent(capture) || capture.modelIds !== JSON.stringify(settings().model_ids)) return;
      const result = body.vectors || {};
      if (result.library_id !== capture.libraryId || result.snapshot_id !== capture.snapshotId) {
        throw new Error("向量操作结果身份无效，未应用结果。");
      }
      if (action === "preview") {
        if (plan) plan.textContent = `待发送 ${result.new_inputs ?? 0} 个文本块、${result.new_input_chars ?? 0} 字符；预计最多 ${result.planned_calls ?? 0} 次调用；可复用 ${result.reused_inputs ?? 0} 个。`;
        local.vectorPreviewReady = true;
        if (previewButton) previewButton.disabled = false;
        if (buildButton) buildButton.disabled = false;
      } else {
        if (plan) plan.textContent = `向量已准备。实际发送 ${result.provider_audit?.call_count ?? result.call_count ?? 0} 次；没有自动重试。`;
        if (previewButton) previewButton.disabled = false;
        if (buildButton) buildButton.disabled = true;
      }
    } catch (error) {
      if (!vectorCurrent(capture)) return;
      const audit = error.payload?.provider_audit;
      const tokens = audit?.calls?.reduce((sum, call) => sum + (call.usage?.total_tokens || 0), 0) || 0;
      if (plan) plan.textContent = `${error.message}${audit ? ` 已发送 ${audit.call_count} 次${tokens ? `，已观测 ${tokens} tokens` : ""}，可能计费；没有自动重试。` : ""}`;
      if (previewButton) previewButton.disabled = false;
      if (buildButton) buildButton.disabled = true;
    } finally {
      if (action === "build" && vectorCurrent(capture)) await app.loadSnapshots();
    }
  }

  async function copyValue(id) {
    const value = document.querySelector(`#${id}`)?.value || "";
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      app.toast("已复制；未执行，也未写入配置");
    } catch {
      app.toast("浏览器未允许复制，请手工选择文本");
    }
  }

  async function handleAction(action) {
    if (action === "open-apps") { app.state.page = "apps"; app.render(); }
    else if (action === "open-settings") { app.state.page = "settings"; app.render(); }
    else if (action === "save-aliyun-key") await saveCredential("aliyun", "aliyun-secret");
    else if (action === "save-openai-key") await saveCredential("openai_tunnel", "openai-secret");
    else if (action === "save-models") await saveModels();
    else if (action === "recommend-models") await restoreRecommended();
    else if (action === "check-models") await checkModels();
    else if (action === "copy-mcp-cli") await copyValue("mcp-cli");
    else if (action === "copy-mcp-toml") await copyValue("mcp-toml");
    else if (action === "mcp-self-check") await runMcpSelfCheck();
    else if (action === "save-real-tunnel") await saveTunnelSettings();
    else if (action.startsWith("real-tunnel-")) await runRealTunnel(action.slice("real-tunnel-".length));
    else if (action.startsWith("simulated-")) await runSimulatedTunnel(action.slice("simulated-".length));
    else if (action === "preview-vectors") await vectors("preview");
    else if (action === "build-vectors") await vectors("build");
  }

  function handleChange(event) {
    if (event.target.id === "enhanced-search") saveModels(Boolean(event.target.checked));
  }

  function handleInput(event) {
    const role = event.target.dataset?.modelRole;
    if (role && roles.some(item => item[0] === role)) {
      local.modelDraft = {...(local.modelDraft || settings().model_ids), [role]: event.target.value};
    }
  }

  globalThis.FolioConnections = {
    local,
    revisions,
    acceptStatus,
    enhancedAvailable,
    enhancedEnabled,
    setEnhancedEnabled,
    renderSettings,
    renderApps,
    handleAction,
    handleChange,
    handleInput,
    showVectorDialog,
    vectors,
    saveModels,
    restoreRecommended,
    checkModels,
    runMcpSelfCheck,
    runRealTunnel,
    runSimulatedTunnel,
  };
  if (app.state.status) acceptStatus(app.state.status);
})();
