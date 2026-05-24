const state = {
  config: null,
  polling: null,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload.detail || JSON.stringify(payload);
    } catch (error) {
      detail = await response.text();
    }
    throw new Error(detail);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setStatus(message, type = "idle") {
  const badge = document.querySelector("#status-badge");
  badge.textContent = message;
  badge.className = `badge ${type}`;
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function toast(message) {
  window.alert(message);
}

function renderConfig(config) {
  state.config = config;
  document.querySelector("#comfyui-base-url").value = config.comfyui_base_url || "";
  document.querySelector("#comfyui-output-dir").value = config.comfyui_output_dir || "";
  document.querySelector("#save-prefix-root").value = config.save_prefix_root || "";
  document.querySelector("#upload-subfolder").value = config.upload_subfolder || "";
  document.querySelector("#seed-base").value = config.seed_base || 1;
  document.querySelector("#poll-interval-seconds").value = config.poll_interval_seconds || 5;
  document.querySelector("#timeout-seconds").value = config.timeout_seconds || 3600;
  document.querySelector("#image-node-id").value = config.workflow_nodes?.image?.id || "";
  document.querySelector("#image-input-name").value = config.workflow_nodes?.image?.input_name || "image";
  document.querySelector("#upload-input-name").value = config.workflow_nodes?.image?.upload_input_name || "upload";
  document.querySelector("#upload-value").value = config.workflow_nodes?.image?.upload_value || "image";
  document.querySelector("#prompt-node-id").value = config.workflow_nodes?.positive_prompt?.id || "";
  document.querySelector("#prompt-input-name").value = config.workflow_nodes?.positive_prompt?.input_name || "text";
  document.querySelector("#save-node-id").value = config.workflow_nodes?.save_video?.id || "";
  document.querySelector("#save-input-name").value = config.workflow_nodes?.save_video?.input_name || "filename_prefix";
  document.querySelector("#seed-nodes-json").value = JSON.stringify(
    config.workflow_nodes?.seed_nodes || [],
    null,
    2
  );
}

function buildConfigFromForm() {
  const config = deepClone(state.config || {});
  config.comfyui_base_url = document.querySelector("#comfyui-base-url").value.trim();
  config.comfyui_output_dir = document.querySelector("#comfyui-output-dir").value.trim();
  config.save_prefix_root = document.querySelector("#save-prefix-root").value.trim();
  config.upload_subfolder = document.querySelector("#upload-subfolder").value.trim();
  config.seed_base = Number(document.querySelector("#seed-base").value || "1");
  config.poll_interval_seconds = Number(document.querySelector("#poll-interval-seconds").value || "5");
  config.timeout_seconds = Number(document.querySelector("#timeout-seconds").value || "3600");
  config.workflow_nodes = config.workflow_nodes || {};
  config.workflow_nodes.image = {
    id: document.querySelector("#image-node-id").value.trim(),
    input_name: document.querySelector("#image-input-name").value.trim(),
    upload_input_name: document.querySelector("#upload-input-name").value.trim(),
    upload_value: document.querySelector("#upload-value").value.trim(),
  };
  config.workflow_nodes.positive_prompt = {
    id: document.querySelector("#prompt-node-id").value.trim(),
    input_name: document.querySelector("#prompt-input-name").value.trim(),
  };
  config.workflow_nodes.save_video = {
    id: document.querySelector("#save-node-id").value.trim(),
    input_name: document.querySelector("#save-input-name").value.trim(),
  };
  config.workflow_nodes.seed_nodes = JSON.parse(
    document.querySelector("#seed-nodes-json").value || "[]"
  );
  return config;
}

function renderCells(cells) {
  const root = document.querySelector("#cells-grid");
  if (!cells.length) {
    root.innerHTML = '<p class="empty">还没有切好的 cell 图片。</p>';
    return;
  }
  root.innerHTML = cells
    .map(
      (cell) => `
        <article class="cell-card">
          <img src="${cell.url}" alt="${cell.name}">
          <div class="cell-meta">
            <strong>${cell.name}</strong>
            <span>#${cell.index}</span>
          </div>
        </article>
      `
    )
    .join("");
}

function renderOutputs(outputs) {
  const root = document.querySelector("#outputs-list");
  if (!outputs.length) {
    root.innerHTML = '<p class="empty">还没有生成好的视频。</p>';
    return;
  }
  root.innerHTML = outputs
    .map(
      (item) => `
        <article class="output-item">
          <div>
            <a href="${item.url}" target="_blank" rel="noreferrer">${escapeHtml(item.name)}</a>
            <div>${escapeHtml(item.modified_at)}</div>
          </div>
          <div>${Math.round(item.size_bytes / 1024)} KB</div>
        </article>
      `
    )
    .join("");
}

function renderFailedJobs(items) {
  document.querySelector("#failed-box").textContent = JSON.stringify(items, null, 2);
}

function renderLogs(logs) {
  document.querySelector("#logs-box").textContent = logs.join("\n");
}

function renderJobs(items) {
  const tbody = document.querySelector("#jobs-body");
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="5">还没有任务记录。</td></tr>';
    return;
  }
  tbody.innerHTML = items
    .map(
      (item) => `
        <tr>
          <td>${escapeHtml(item.index)}</td>
          <td>${escapeHtml(item.status)}</td>
          <td>${escapeHtml(item.output_name || "")}</td>
          <td>${escapeHtml(item.prompt_id || "")}</td>
          <td>${escapeHtml(item.error || "")}</td>
        </tr>
      `
    )
    .join("");
}

function renderSummary(progress, totalOverride = null) {
  const total = totalOverride ?? progress.total ?? 0;
  const done =
    (progress.completed || 0) +
    (progress.failed || 0) +
    (progress.skipped || 0) +
    (progress.cancelled || 0);
  const percent = total ? Math.round((done / total) * 100) : 0;
  document.querySelector("#progress-bar").style.width = `${percent}%`;
  document.querySelector("#summary-cards").innerHTML = `
    <div class="summary-card"><span>Total</span><strong>${total}</strong></div>
    <div class="summary-card"><span>Queued</span><strong>${progress.queued || 0}</strong></div>
    <div class="summary-card"><span>Running</span><strong>${progress.running || 0}</strong></div>
    <div class="summary-card"><span>Completed</span><strong>${progress.completed || 0}</strong></div>
    <div class="summary-card"><span>Failed</span><strong>${progress.failed || 0}</strong></div>
    <div class="summary-card"><span>Skipped</span><strong>${progress.skipped || 0}</strong></div>
    <div class="summary-card"><span>Cancelled</span><strong>${progress.cancelled || 0}</strong></div>
  `;
}

function renderRunControls(batchState) {
  const startButton = document.querySelector("#start-run-btn");
  const stopButton = document.querySelector("#stop-run-btn");
  const running = Boolean(batchState.running);
  const stopping = (batchState.phase || "") === "stopping";
  startButton.disabled = running;
  stopButton.disabled = !running || stopping;
}

function renderBatchState(batchState) {
  const phase = batchState.phase || "idle";
  const current = batchState.current_output_name ? ` · ${batchState.current_output_name}` : "";
  setStatus(`${phase.toUpperCase()}${current}`, phase);
  renderLogs(batchState.logs || []);
  renderJobs(batchState.items || []);
  renderSummary(batchState.progress || {}, batchState.progress?.total || 0);
  renderRunControls(batchState);
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  renderConfig(data.config);
  document.querySelector("#prompts-text").value = data.prompts_text;
  renderCells(data.cells);
  renderOutputs(data.outputs);
  renderFailedJobs(data.failed_jobs);
  renderBatchState(data.batch_state);
}

async function pollBatchState() {
  try {
    const data = await api("/api/run/status");
    renderOutputs(data.outputs);
    renderFailedJobs(data.failed_jobs);
    renderBatchState(data.batch_state);
  } catch (error) {
    console.error(error);
  }
}

function ensurePolling() {
  if (state.polling) {
    window.clearInterval(state.polling);
  }
  state.polling = window.setInterval(pollBatchState, 3000);
}

async function handleStoryboardSubmit(event) {
  event.preventDefault();
  const file = document.querySelector("#storyboard-file").files[0];
  if (!file) {
    toast("先选择 storyboard PNG。");
    return;
  }

  const form = new FormData();
  form.append("file", file);
  form.append("rows", document.querySelector("#rows").value);
  form.append("cols", document.querySelector("#cols").value);
  form.append("margin", document.querySelector("#margin").value);
  form.append("gutter", document.querySelector("#gutter").value);

  try {
    const data = await api("/api/storyboard", { method: "POST", body: form });
    renderCells(data.cells);
    toast("Storyboard 已上传并切图完成。");
  } catch (error) {
    toast(error.message);
  }
}

async function uploadJsonFile(inputSelector, path) {
  const file = document.querySelector(inputSelector).files[0];
  if (!file) {
    toast("先选择 JSON 文件。");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  await api(path, { method: "POST", body: form });
}

async function savePrompts() {
  const text = document.querySelector("#prompts-text").value;
  await api("/api/prompts", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

async function saveConfig() {
  const config = buildConfigFromForm();
  const data = await api("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  state.config = data.config;
}

async function validateWorkflow() {
  const data = await api("/api/workflow/validate");
  document.querySelector("#validation-box").textContent = JSON.stringify(data.bindings, null, 2);
}

async function startBatchRun() {
  const payload = {
    start_index: Number(document.querySelector("#start-index").value || "1"),
    end_index: Number(document.querySelector("#end-index").value || "12"),
    overwrite: document.querySelector("#overwrite").checked,
  };
  await api("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function stopBatchRun() {
  return api("/api/run/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

function bindEvents() {
  document.querySelector("#storyboard-form").addEventListener("submit", handleStoryboardSubmit);
  document.querySelector("#upload-prompts-btn").addEventListener("click", async () => {
    try {
      await uploadJsonFile("#prompts-file", "/api/prompts/upload");
      await loadDashboard();
      toast("prompts.json 上传完成。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#save-prompts-btn").addEventListener("click", async () => {
    try {
      await savePrompts();
      toast("prompts 已保存。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#upload-workflow-btn").addEventListener("click", async () => {
    try {
      await uploadJsonFile("#workflow-file", "/api/workflow/upload");
      toast("workflow 已上传。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#save-config-btn").addEventListener("click", async () => {
    try {
      await saveConfig();
      toast("配置已保存。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#validate-workflow-btn").addEventListener("click", async () => {
    try {
      await saveConfig();
      await validateWorkflow();
      toast("workflow 校验通过。");
    } catch (error) {
      document.querySelector("#validation-box").textContent = error.message;
      toast(error.message);
    }
  });
  document.querySelector("#start-run-btn").addEventListener("click", async () => {
    try {
      await saveConfig();
      await savePrompts();
      await startBatchRun();
      toast("批处理已启动。");
      await pollBatchState();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#stop-run-btn").addEventListener("click", async () => {
    try {
      const data = await stopBatchRun();
      const warnings = data.warnings || [];
      if (warnings.length) {
        toast(`已请求停止，但有告警：\n${warnings.join("\n")}`);
      } else {
        toast("已请求停止批处理。");
      }
      await pollBatchState();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#refresh-btn").addEventListener("click", loadDashboard);
}

async function main() {
  bindEvents();
  await loadDashboard();
  ensurePolling();
}

main().catch((error) => {
  console.error(error);
  toast(error.message);
});
