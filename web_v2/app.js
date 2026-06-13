const state = {
  dashboard: null,
  selectedDraft: null,
  selectedRunId: "",
  selectedBatchId: "",
  queueView: "dashboard",
  polling: null,
  settingsHydrated: false,
  promptsFileInfo: { count: null, error: "", loading: false },
  accessInfo: null,
  runtimeProfileStatus: null,
  lastRenderedDraftId: "",
  lastRenderedDraftUpdatedAt: "",
};

const RUN_STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  stopping: "停止中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  cancelled: "已取消",
  unknown: "未知",
  interrupted: "已中断",
};

const TASK_STATUS_LABELS = {
  pending: "待提交",
  queued: "已入队",
  running: "生成中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "未知",
  interrupted: "已中断",
};

const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "stopping"]);
const PLANNED_BATCH_STATUSES = new Set(["planned", "scheduled"]);

const PROMPT_JSON_FORMATS = [
  {
    title: "单条 JSON 字符串",
    description: "只生成一个视频时可用。",
    value: "一条完整的视频画面提示词",
  },
  {
    title: "字符串数组",
    description: "最简批量格式，每个字符串是一条任务。",
    value: ["第一条完整的视频画面提示词", "第二条完整的视频画面提示词"],
  },
  {
    title: "Prompt 对象数组",
    description: "推荐格式，只需要 prompt 字段。",
    value: [
      { prompt: "第一条完整的视频画面提示词" },
      { prompt: "第二条完整的视频画面提示词" },
    ],
  },
  {
    title: "单个 Prompt 对象",
    description: "只生成一个视频时也可以直接使用对象。",
    value: { prompt: "一条完整的视频画面提示词" },
  },
  {
    title: "常见 Prompt 字段变体",
    description: "兼容 text、prompt1、Prompt_2、visual_prompt、video_prompt 和 positive_prompt。",
    value: [
      { text: "第一条完整的视频画面提示词" },
      { prompt1: "第二条完整的视频画面提示词" },
      { Prompt_2: "第三条完整的视频画面提示词" },
      { video_prompt: "第四条完整的视频画面提示词" },
    ],
  },
  {
    title: "带可选信息的对象数组",
    description: "可附带序号、字幕和输出文件名；只有 prompt 会传给 ComfyUI。",
    value: [
      { index: 1, prompt: "第一条完整的视频画面提示词", subtitle: "第一条字幕", output_name: "01_scene.mp4" },
      { index: 2, prompt: "第二条完整的视频画面提示词", subtitle: "第二条字幕", output_name: "02_scene.mp4" },
    ],
  },
  {
    title: "Items 包装格式",
    description: "用 items 包装字符串或 prompt/text 对象。",
    value: {
      items: [
        "第一条完整的视频画面提示词",
        { text: "第二条完整的视频画面提示词" },
      ],
    },
  },
  {
    title: "Master Prompt + Scenes 字符串",
    description: "公共风格会自动拼到每一条 scene 前面。",
    value: {
      master_prompt: "所有镜头统一使用电影级光影和写实质感。",
      scenes: ["第一条镜头内容", "第二条镜头内容"],
    },
  },
  {
    title: "Master Prompt + Scenes 对象",
    description: "scene 对象支持 prompt、visual_prompt 或 text 字段。",
    value: {
      master_prompt: "所有镜头统一使用电影级光影和写实质感。",
      scenes: [
        { prompt: "第一条镜头内容", subtitle: "第一条字幕" },
        { visual_prompt: "第二条镜头内容", output_name: "02_scene.mp4" },
        { text: "第三条镜头内容" },
      ],
    },
  },
];

function isPlanBatch(batch) {
  return PLANNED_BATCH_STATUSES.has(batch?.status || "");
}

function initAccessToken() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("access_token") || params.get("token");
  if (!token) {
    return;
  }
  window.localStorage.setItem("batchStudioAccessToken", token);
  params.delete("access_token");
  params.delete("token");
  const query = params.toString();
  window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
}

function accessToken() {
  return window.localStorage.getItem("batchStudioAccessToken") || "";
}

function withAccessToken(url) {
  const token = accessToken();
  if (!token || !url) {
    return url;
  }
  const target = new URL(url, window.location.origin);
  target.searchParams.set("access_token", token);
  return target.origin === window.location.origin ? `${target.pathname}${target.search}${target.hash}` : target.toString();
}

async function api(path, options = {}, retryAuth = true) {
  const headers = new Headers(options.headers || {});
  const token = accessToken();
  if (token) {
    headers.set("X-Batch-Studio-Token", token);
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && retryAuth) {
    const nextToken = window.prompt("这个工作台需要访问令牌。请输入启动时设置的 token：");
    if (nextToken) {
      window.localStorage.setItem("batchStudioAccessToken", nextToken.trim());
      return api(path, options, false);
    }
  }
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

function toast(message) {
  window.alert(message);
}

async function copyText(text) {
  let copied = false;
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (error) {
      copied = false;
    }
  }
  if (!copied) {
    const fallback = document.createElement("textarea");
    fallback.value = text;
    fallback.setAttribute("readonly", "");
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.appendChild(fallback);
    fallback.select();
    copied = document.execCommand("copy");
    fallback.remove();
  }
  if (!copied) {
    throw new Error("浏览器没有授予剪贴板权限");
  }
}

function promptFormatText(format) {
  return JSON.stringify(format.value, null, 2);
}

function renderPromptFormats() {
  const list = document.querySelector("#prompt-formats-list");
  list.innerHTML = PROMPT_JSON_FORMATS.map((format, index) => `
    <article class="prompt-format-card">
      <header>
        <div>
          <strong>${escapeHtml(format.title)}</strong>
          <p>${escapeHtml(format.description)}</p>
        </div>
        <button type="button" class="prompt-format-copy" data-prompt-format-copy="${index}" title="复制 ${escapeHtml(format.title)}" aria-label="复制 ${escapeHtml(format.title)}">
          <span class="material-symbols-outlined">content_copy</span>
        </button>
      </header>
      <pre class="code-box">${escapeHtml(promptFormatText(format))}</pre>
      <small class="prompt-format-copy-status" data-prompt-format-status="${index}" aria-live="polite"></small>
    </article>
  `).join("");
}

function openPromptFormats() {
  renderPromptFormats();
  document.querySelector("#prompt-formats-modal").classList.remove("hidden");
}

function closePromptFormats() {
  document.querySelector("#prompt-formats-modal")?.classList.add("hidden");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function currentProjectDetail() {
  return state.dashboard?.selected_project || null;
}

function currentProject() {
  return currentProjectDetail()?.project || null;
}

function currentProfiles() {
  return currentProjectDetail()?.profiles || [];
}

function currentRuns() {
  return currentProjectDetail()?.runs || [];
}

function currentBatches() {
  return currentProjectDetail()?.batches || [];
}

function findRun(runId) {
  return currentRuns().find((run) => run.id === runId) || null;
}

function findBatch(batchId) {
  return currentBatches().find((batch) => batch.id === batchId) || null;
}

function fileList(selector) {
  return Array.from(document.querySelector(selector)?.files || []);
}

function selectedFileName(selector) {
  return fileList(selector)[0]?.name || "";
}

function parseOptionalInteger(selector) {
  const raw = document.querySelector(selector)?.value.trim() || "";
  if (!raw) {
    return null;
  }
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatSeconds(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) {
    return "-";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${rest}s`;
}

function percent(value, total) {
  if (!total) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round((value / total) * 100)));
}

function normalizeSaveSubfolder(value) {
  const raw = String(value || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
  return raw || "batch_studio_v2";
}

function selectedProfile() {
  const profileId = document.querySelector("#draft-profile-id").value;
  return currentProfiles().find((profile) => profile.id === profileId) || null;
}

function profileNameFor(profileId) {
  const profile = currentProfiles().find((item) => item.id === profileId);
  return profile?.name || profileId || "-";
}

function runtimeSettingsFor(item) {
  return item?.run_settings || item?.runtime_settings || {};
}

function savePathLabel(settings = {}) {
  const savePrefix = settings.save_prefix_root || "";
  const finalOutput = settings.final_output_dir || "";
  if (finalOutput) {
    return `最终 ${finalOutput}`;
  }
  if (savePrefix) {
    return `ComfyUI ${savePrefix}`;
  }
  return "-";
}

function batchMetaLine(item) {
  const settings = runtimeSettingsFor(item);
  const duration = settings.duration_seconds ? `${settings.duration_seconds}s` : "-";
  const savePath = savePathLabel(settings);
  return `Workflow: ${profileNameFor(item?.profile_id)} | 视频时长: ${duration} | 保存: ${savePath}`;
}

function countPromptPayload(payload) {
  if (typeof payload === "string") {
    return payload.trim() ? 1 : 0;
  }
  if (Array.isArray(payload)) {
    return payload.length;
  }
  if (payload && typeof payload === "object" && Array.isArray(payload.items)) {
    return payload.items.length;
  }
  if (payload && typeof payload === "object" && Array.isArray(payload.scenes)) {
    return payload.scenes.length;
  }
  if (payload && typeof payload === "object" && Object.keys(payload).some(isPromptFieldName)) {
    return 1;
  }
  return null;
}

function isPromptFieldName(key) {
  const normalized = String(key || "").trim().toLowerCase().replace(/[\s_-]+/g, "");
  return /^(prompt|text|visualprompt|videoprompt|positiveprompt)\d*$/.test(normalized);
}

function stripLoosePromptPrefix(line) {
  return String(line || "")
    .trim()
    .replace(/^\s*(?:[-*•]+|\d+[\.\)、)]|[（(]\d+[）)])\s*/, "")
    .trim()
    .replace(/^["']+|["']+$/g, "")
    .trim();
}

function loosePromptLines(text) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map(stripLoosePromptPrefix)
    .filter(Boolean);
  if (lines.length) {
    return lines;
  }
  const cleaned = stripLoosePromptPrefix(text);
  return cleaned ? [cleaned] : [];
}

function parsePromptPayloadText(text) {
  const raw = String(text || "").trim().replace(/^\uFEFF/, "");
  if (!raw) {
    throw new Error("prompts text is required.");
  }
  const normalized = raw.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
  const candidates = raw === normalized ? [raw] : [raw, normalized];
  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch (error) {
      // Try the next lenient form.
    }
  }
  const trailingCommaFixed = normalized.replace(/,\s*([}\]])/g, "$1");
  if (trailingCommaFixed !== normalized) {
    try {
      return JSON.parse(trailingCommaFixed);
    } catch (error) {
      // Fall through to loose text parsing.
    }
  }
  if (!normalized.startsWith("[") && !normalized.startsWith("{")) {
    try {
      return JSON.parse(`[${normalized}]`);
    } catch (error) {
      return loosePromptLines(normalized);
    }
  }
  throw new Error("looks like JSON but could not be parsed");
}

function countPromptsFromText(text) {
  return countPromptPayload(parsePromptPayloadText(text));
}

function promptInputDetails() {
  const text = document.querySelector("#prompts-text").value.trim();
  const fileName = selectedFileName("#prompts-file");
  if (fileName) {
    if (state.promptsFileInfo.loading) {
      return { ready: false, count: null, text: `正在读取 prompts 文件：${fileName}`, error: "" };
    }
    if (state.promptsFileInfo.error) {
      return { ready: false, count: null, text: `prompts 文件无法解析：${state.promptsFileInfo.error}`, error: state.promptsFileInfo.error };
    }
    const count = state.promptsFileInfo.count;
    return {
      ready: true,
      count,
      text: count === null ? `已选择 prompts 文件：${fileName}` : `已选择 prompts 文件：${fileName}，${count} 条`,
      error: "",
    };
  }
  if (text) {
    try {
      const count = countPromptsFromText(text);
      return {
        ready: true,
        count,
        text: count === null ? "已粘贴 prompts 文本" : `已粘贴 prompts 文本，${count} 条`,
        error: "",
      };
    } catch (error) {
      return { ready: false, count: null, text: `粘贴的 prompts 不是合法 JSON：${error.message}`, error: error.message };
    }
  }
  return { ready: false, count: null, text: "还没有提供 prompts", error: "" };
}

async function updatePromptFileInfo() {
  const file = fileList("#prompts-file")[0];
  if (!file) {
    state.promptsFileInfo = { count: null, error: "", loading: false };
    renderUploadChecklist();
    return;
  }
  state.promptsFileInfo = { count: null, error: "", loading: true };
  renderUploadChecklist();
  try {
    const text = await file.text();
    state.promptsFileInfo = { count: countPromptsFromText(text), error: "", loading: false };
  } catch (error) {
    state.promptsFileInfo = { count: null, error: error.message, loading: false };
  }
  renderUploadChecklist();
}

function runtimeOverrides(options = {}) {
  const seedMode = document.querySelector("#seed-mode").value;
  const fixedSeed = Number(document.querySelector("#seed-fixed").value || "1");
  const overrides = {
    seed_mode: seedMode,
    seed_fixed: fixedSeed,
    seed_base: fixedSeed,
    save_prefix_root: normalizeSaveSubfolder(document.querySelector("#comfyui-output-dir").value),
    output_name_prefix: document.querySelector("#output-name-prefix").value.trim(),
    repeat_count: parseOptionalInteger("#repeat-count") || 1,
    width_pixels: parseOptionalInteger("#width-pixels"),
    height_pixels: parseOptionalInteger("#height-pixels"),
    duration_seconds: parseOptionalInteger("#duration-seconds"),
    maintenance_interval_tasks: parseOptionalInteger("#maintenance-interval-tasks"),
    maintenance_cooldown_seconds: parseOptionalInteger("#maintenance-cooldown-seconds"),
    maintenance_memory_mode: "free_memory",
  };
  if (options.includeDraftMode) {
    overrides.draft_mode = document.querySelector("#draft-mode").value || "t2v";
  }
  return overrides;
}

function settingsPayload() {
  return {
    comfyui_base_url: document.querySelector("#comfyui-base-url").value.trim(),
    save_prefix_root: normalizeSaveSubfolder(document.querySelector("#comfyui-output-dir").value),
    ...runtimeOverrides(),
  };
}

function workflowConfigHint() {
  const saveNodeId = document.querySelector("#save-node-id").value.trim();
  const saveInputName = document.querySelector("#save-input-name").value.trim() || "filename_prefix";
  if (!saveNodeId) {
    return {};
  }
  return {
    workflow_nodes: {
      save_video: {
        id: saveNodeId,
        input_name: saveInputName,
      },
    },
  };
}

async function saveSettings() {
  const data = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settingsPayload()),
  });
  document.querySelector("#settings-status").textContent = "运行参数已保存。";
  return data.project;
}

function hydrateSettings(projectDetail) {
  const project = projectDetail?.project || {};
  const defaults = project.default_run_settings || {};
  let saveSubfolder = defaults.save_prefix_root || "batch_studio_v2";
  if (/^[A-Za-z]:/.test(saveSubfolder) || saveSubfolder.includes("\\")) {
    saveSubfolder = "batch_studio_v2";
  }
  document.querySelector("#comfyui-base-url").value = project.comfyui?.base_url || "http://127.0.0.1:8189";
  document.querySelector("#comfyui-output-dir").value = normalizeSaveSubfolder(saveSubfolder);
  document.querySelector("#output-name-prefix").value = defaults.output_name_prefix || "";
  document.querySelector("#seed-mode").value = defaults.seed_mode || "random";
  document.querySelector("#seed-fixed").value = defaults.seed_fixed || defaults.seed_base || 1;
  document.querySelector("#repeat-count").value = defaults.repeat_count || 1;
  document.querySelector("#width-pixels").value = defaults.width_pixels || "1280";
  document.querySelector("#height-pixels").value = defaults.height_pixels || "720";
  document.querySelector("#duration-seconds").dataset.savedValue = defaults.duration_seconds || "";
  document.querySelector("#duration-seconds").value = defaults.duration_seconds || "";
  document.querySelector("#maintenance-interval-tasks").value = defaults.maintenance_interval_tasks ?? 5;
  document.querySelector("#maintenance-cooldown-seconds").value = defaults.maintenance_cooldown_seconds ?? 20;
  syncSizePreset();
  updateSeedFieldVisibility();
}

function showRuntimeOutput(text) {
  const box = document.querySelector("#comfyui-runtime-output");
  if (!box) {
    return;
  }
  box.textContent = text || "";
  box.classList.toggle("hidden", !text);
}

function renderRuntimeProfileStatus(payload) {
  state.runtimeProfileStatus = payload;
  const select = document.querySelector("#comfyui-runtime-profile");
  const status = document.querySelector("#comfyui-runtime-status");
  if (!select || !status || !payload) {
    return;
  }
  if (payload.profiles?.[payload.current_profile]) {
    select.value = payload.current_profile;
  }
  const current = payload.profiles?.[payload.current_profile];
  const target = payload.target || {};
  const targetText = target.known ? target.label || target.id : "未登记容器";
  const gpuText = payload.nvidia_smi ? `GPU ${payload.nvidia_smi}` : "GPU 状态未知";
  const dockerText = payload.docker_available ? "Docker 可用" : "Docker 未连接";
  const comfyText = payload.comfyui_reachable ? "ComfyUI 可达" : "ComfyUI 不可达";
  status.textContent = `${targetText} | ${current?.label || "未应用模式"} | ${dockerText} | ${comfyText} | ${gpuText}`;
}

async function loadRuntimeProfileStatus() {
  const baseUrl = document.querySelector("#comfyui-base-url")?.value.trim() || "";
  const query = baseUrl ? `?comfyui_base_url=${encodeURIComponent(baseUrl)}` : "";
  const payload = await api(`/api/comfyui/runtime-profiles${query}`);
  renderRuntimeProfileStatus(payload);
  return payload;
}

async function applyRuntimeProfile(force = false) {
  const select = document.querySelector("#comfyui-runtime-profile");
  const profile = select?.value || "balanced";
  const label = select?.selectedOptions?.[0]?.textContent || profile;
  if (!force && !window.confirm(`应用「${label}」会重建 ComfyUI Docker 容器，正在生成的 ComfyUI 任务会中断。确认继续吗？`)) {
    return;
  }
  showRuntimeOutput("正在切换 ComfyUI Docker 模式，请稍等...");
  try {
    const payload = await api("/api/comfyui/runtime-profiles/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, force, comfyui_base_url: document.querySelector("#comfyui-base-url").value.trim() }),
    });
    renderRuntimeProfileStatus(payload.status);
    const result = payload.result || {};
    showRuntimeOutput([result.stdout, result.stderr].filter(Boolean).join("\n").trim() || "已切换。");
  } catch (error) {
    if (!force && String(error.message || "").includes("Batch queue is not empty")) {
      if (window.confirm("工作台队列不是空的。强制切换会中断/影响正在排队的任务，仍然继续吗？")) {
        await applyRuntimeProfile(true);
        return;
      }
    }
    showRuntimeOutput(error.message);
    throw error;
  }
}

async function diagnoseComfyUIRuntime() {
  showRuntimeOutput("正在诊断 Docker / GPU / ComfyUI 日志...");
  const payload = await api("/api/comfyui/runtime-profiles/diagnose", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comfyui_base_url: document.querySelector("#comfyui-base-url").value.trim() }),
  });
  const result = payload.result || {};
  showRuntimeOutput([result.stdout, result.stderr].filter(Boolean).join("\n").trim() || "没有诊断输出。");
  await loadRuntimeProfileStatus().catch(console.error);
}

function setActiveTab(tabName) {
  const active = tabName === "queue" ? "queue" : "submit";
  document.body.dataset.activeTab = active;
  document.querySelectorAll(".top-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === active);
  });
  document.querySelector("#submit-panel").classList.toggle("hidden", active !== "submit");
  document.querySelector("#queue-panel").classList.toggle("hidden", active !== "queue");
  window.localStorage.setItem("batchStudioActiveTab", active);
}

function setQueueView(viewName) {
  const view = viewName === "planbox" ? "planbox" : "dashboard";
  state.queueView = view;
  document.querySelectorAll("[data-queue-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.queueView === view);
  });
  document.querySelector("#queue-dashboard-view").classList.toggle("hidden", view !== "dashboard");
  document.querySelector("#queue-planbox-view").classList.toggle("hidden", view !== "planbox");
  window.localStorage.setItem("batchStudioQueueView", view);
}

function setActiveMode(mode) {
  const nextMode = mode || "t2v";
  document.querySelector("#draft-mode").value = nextMode;
  document.querySelectorAll(".mode-pill").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === nextMode);
  });
  window.localStorage.setItem("batchStudioDraftMode", nextMode);
  renderModeFields();
}

function updateSeedFieldVisibility() {
  const fixed = document.querySelector("#seed-mode").value === "fixed";
  document.querySelector("#seed-fixed-field").classList.toggle("seed-placeholder", !fixed);
}

function applySizePreset() {
  const value = document.querySelector("#size-preset").value;
  if (value === "custom") {
    return;
  }
  const [width, height] = value.split("x");
  document.querySelector("#width-pixels").value = width;
  document.querySelector("#height-pixels").value = height;
}

function syncSizePreset() {
  const width = document.querySelector("#width-pixels").value;
  const height = document.querySelector("#height-pixels").value;
  const preset = `${width}x${height}`;
  const select = document.querySelector("#size-preset");
  const hasPreset = Array.from(select.options).some((option) => option.value === preset);
  select.value = hasPreset ? preset : "custom";
}

function durationOptionsForProfile(profile) {
  const schemaItem = (profile?.runtime_schema || []).find((item) => item.key === "duration_seconds");
  const options = new Set([3, 5, 8, 10, 15]);
  for (const value of schemaItem?.options || []) {
    const number = Number.parseInt(value, 10);
    if (Number.isFinite(number) && number > 0) {
      options.add(number);
    }
  }
  const defaultValue = Number.parseInt(schemaItem?.default || profile?.defaults?.duration_seconds || "", 10);
  if (Number.isFinite(defaultValue) && defaultValue > 0) {
    options.add(defaultValue);
  }
  return {
    detected: Boolean(schemaItem),
    values: Array.from(options).sort((a, b) => a - b),
    defaultValue: Number.isFinite(defaultValue) && defaultValue > 0 ? defaultValue : null,
  };
}

function renderDurationOptions(profile) {
  const select = document.querySelector("#duration-seconds");
  const help = document.querySelector("#duration-help");
  if (!select) {
    return;
  }
  const previous = select.value || select.dataset.savedValue || "";
  const duration = durationOptionsForProfile(profile);
  select.innerHTML = duration.values.map((seconds) => `<option value="${seconds}">${seconds}s</option>`).join("");
  if (previous && Array.from(select.options).some((option) => option.value === String(previous))) {
    select.value = previous;
  } else if (duration.defaultValue) {
    select.value = String(duration.defaultValue);
  } else if (duration.values.length) {
    select.value = String(duration.values.includes(5) ? 5 : duration.values[0]);
  }
  if (help) {
    help.textContent = duration.detected
      ? `已识别时长控制。可选预设：${duration.values.map((item) => `${item}s`).join(", ")}。`
      : "此 profile 未保存明确的时长节点。这里显示常用预设；运行时仍会尝试写入 workflow 里的 Duration 节点。";
  }
}

function renderProfiles(profiles) {
  const select = document.querySelector("#draft-profile-id");
  const search = (document.querySelector("#workflow-search")?.value || "").trim().toLowerCase();
  const filtered = profiles.filter((profile) => !search || String(profile.name || "").toLowerCase().includes(search));
  if (!profiles.length) {
    select.innerHTML = '<option value="">还没有 workflow profile</option>';
    document.querySelector("#profiles-box").textContent = "上传 workflow JSON 后，它会保存在这里，下次远程操作时直接选择即可。";
    return;
  }
  const options = filtered.length ? filtered : profiles;
  const previous = select.value;
  const projectDefault = currentProject()?.default_profile_id || "";
  select.innerHTML = options
    .map((profile) => `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.name)}</option>`)
    .join("");
  if (options.some((profile) => profile.id === previous)) {
    select.value = previous;
  } else if (options.some((profile) => profile.id === projectDefault)) {
    select.value = projectDefault;
  }

  const profile = selectedProfile() || options[0];
  renderDurationOptions(profile);
  const mediaInputs = profile?.bindings?.media_inputs || [];
  const saveVideo = profile?.bindings?.save_video || {};
  const duration = durationOptionsForProfile(profile);
  const runtimeFields = (profile?.runtime_schema || []).map((item) => item.label || item.key).join(", ") || "无";
  document.querySelector("#profiles-box").textContent = [
    `当前：${profile?.name || "-"}`,
    `输入类型：${profile?.input_contract?.primary_media_kind || "none"}`,
    `素材输入节点：${mediaInputs.length}`,
    `SaveVideo：${saveVideo.id || "未识别"} / ${saveVideo.input_name || "-"}`,
    `可批量控制参数：${runtimeFields}`,
    `支持时长：${duration.values.map((item) => `${item}s`).join(", ")}`,
  ].join("\n");
}

function taskSummary(run) {
  const tasks = run?.tasks || [];
  const summary = {
    total: tasks.length,
    submitted: 0,
    pending: 0,
    queued: 0,
    running: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    unknown: 0,
    interrupted: 0,
    current_order: null,
    ...(run?.task_summary || {}),
  };
  if (!run?.task_summary) {
    for (const task of tasks) {
      const status = String(task.status || "pending").toLowerCase();
      summary[status] = (summary[status] || 0) + 1;
      if (task.prompt_id) {
        summary.submitted += 1;
      }
      if (summary.current_order === null && ["queued", "running"].includes(status)) {
        summary.current_order = task.order;
      }
    }
  }
  return summary;
}

function setQueueBadge(queueState, runs = []) {
  const badge = document.querySelector("#queue-badge");
  const current = queueState?.current;
  const queued = queueState?.queued || [];
  if (current) {
    const run = runs.find((item) => item.id === current.run_id);
    const summary = taskSummary(run);
    badge.textContent = summary.total ? `运行 ${summary.submitted}/${summary.total} | 待批次 ${queued.length}` : `运行中 | 待批次 ${queued.length}`;
    badge.className = "status-pill running";
  } else if (queued.length) {
    badge.textContent = `待批次 ${queued.length}`;
    badge.className = "status-pill queued";
  } else {
    badge.textContent = "空闲";
    badge.className = "status-pill idle";
  }
}

function modeInfo() {
  const mode = document.querySelector("#draft-mode").value;
  const rows = Number(document.querySelector("#rows").value || "4");
  const cols = Number(document.querySelector("#cols").value || "3");
  const gridCount = rows * cols;
  const requestedCellCount = Number(document.querySelector("#storyboard-cell-count").value || String(gridCount));
  const storyboardCount = Math.max(1, Math.min(requestedCellCount, gridCount));
  const firstBatchCount = fileList("#first-image-files").length;
  const firstCount = fileList("#first-frame-files").length;
  const lastCount = fileList("#last-frame-files").length;

  if (mode === "t2v") {
    return { label: "T2V", requiredMedia: 0, mediaReady: true, expectedTasks: null, text: "只需要 prompts，不需要图片素材。" };
  }
  if (mode === "i2v_storyboard") {
    return {
      label: "I2V 切图",
      requiredMedia: 1,
      mediaReady: Boolean(selectedFileName("#storyboard-file")),
      expectedTasks: storyboardCount,
      text: selectedFileName("#storyboard-file")
        ? `已选择 storyboard。网格 ${rows} x ${cols}，本批取前 ${storyboardCount} 格。`
        : `需要上传一张 storyboard。网格 ${rows} x ${cols}，本批取前 ${storyboardCount} 格。`,
    };
  }
  if (mode === "i2v_first_batch") {
    return {
      label: "I2V 首帧批量",
      requiredMedia: firstBatchCount,
      mediaReady: firstBatchCount > 0,
      expectedTasks: firstBatchCount || null,
      text: firstBatchCount ? `已选择 ${firstBatchCount} 张首帧图。` : "需要批量上传首帧图片。",
    };
  }
  if (mode === "i2v_first_last_batch") {
    return {
      label: "I2V 首尾帧",
      requiredMedia: firstCount + lastCount,
      mediaReady: firstCount > 0 && firstCount === lastCount,
      expectedTasks: firstCount && firstCount === lastCount ? firstCount : null,
      text: firstCount === lastCount && firstCount > 0
        ? `已选择 ${firstCount} 组首尾帧。`
        : `首帧和尾帧数量需要一致。当前首帧 ${firstCount} 张，尾帧 ${lastCount} 张。`,
    };
  }
  return {
    label: "I2V 连续首尾帧",
    requiredMedia: firstCount,
    mediaReady: firstCount >= 2,
    expectedTasks: firstCount >= 2 ? firstCount - 1 : null,
    text: firstCount >= 2
      ? `已选择 ${firstCount} 张连续图，会生成 ${firstCount - 1} 组首尾帧。`
      : "需要上传至少 2 张连续图片，按文件名排序后 1-2、2-3 依次成组。",
  };
}

function syncStoryboardCellCount() {
  const rows = Number(document.querySelector("#rows").value || "4");
  const cols = Number(document.querySelector("#cols").value || "3");
  const maxCells = Math.max(1, rows * cols);
  const input = document.querySelector("#storyboard-cell-count");
  const current = Number(input.value || maxCells);
  input.max = String(maxCells);
  if (!input.value || current > maxCells) {
    input.value = String(maxCells);
  }
}

function renderModeFields() {
  const mode = document.querySelector("#draft-mode").value;
  syncStoryboardCellCount();
  document.querySelector("#storyboard-fields").classList.toggle("hidden", mode !== "i2v_storyboard");
  document.querySelector("#first-batch-fields").classList.toggle("hidden", mode !== "i2v_first_batch");
  document.querySelector("#first-last-fields").classList.toggle("hidden", !["i2v_first_last_batch", "i2v_first_last_continuous"].includes(mode));
  document.querySelector("#last-frame-upload").classList.toggle("hidden", mode === "i2v_first_last_continuous");
  document.querySelector("#continuous-note").classList.toggle("hidden", mode !== "i2v_first_last_continuous");
  renderUploadChecklist();
}

function renderUploadChecklist() {
  const profileReady = Boolean(selectedProfile());
  const promptDetails = promptInputDetails();
  const promptsReady = promptDetails.ready && !promptDetails.error;
  const info = modeInfo();
  const promptCountKnown = promptDetails.count !== null && promptDetails.count !== undefined;
  const countMismatch = info.expectedTasks !== null && promptsReady && promptCountKnown && promptDetails.count !== info.expectedTasks;
  const ready = profileReady && promptsReady && info.mediaReady && !countMismatch;

  const items = [
    {
      tag: profileReady ? "已就绪" : "缺少",
      kind: profileReady ? "ready" : "missing",
      title: "Workflow JSON",
      text: profileReady ? `已选择：${selectedProfile().name}` : "先选择或上传一个 ComfyUI API workflow。",
    },
    {
      tag: promptsReady ? "已就绪" : "缺少",
      kind: promptsReady ? "ready" : "missing",
      title: "Prompts",
      text: promptsReady ? promptDetails.text : promptDetails.text === "还没有提供 prompts" ? "粘贴 prompt JSON，或上传 prompts.json。" : promptDetails.text,
    },
    {
      tag: info.mediaReady ? "已就绪" : "缺少",
      kind: info.mediaReady ? "ready" : "missing",
      title: info.label,
      text: info.text,
    },
  ];

  if (countMismatch) {
    items.push({
      tag: "需调整",
      kind: "warning",
      title: "数量检查",
      text: `素材会生成 ${info.expectedTasks} 个任务，但 prompts 是 ${promptDetails.count} 条。两者需要一致。`,
    });
  } else if (info.expectedTasks !== null && promptCountKnown) {
    items.push({
      tag: "已匹配",
      kind: "ready",
      title: "数量检查",
      text: `素材任务数和 prompts 数量都是 ${info.expectedTasks}。`,
    });
  }

  document.querySelector("#upload-checklist").innerHTML = `
    <div class="upload-check-title">
      <strong>本批次需要</strong>
      <span>${escapeHtml(info.label)}</span>
    </div>
    <div class="upload-status-row">
      ${items.map((item) => `
      <div class="upload-item ${item.kind}" title="${escapeHtml(item.text)}">
        <span class="upload-tag ${item.kind}">${escapeHtml(item.tag)}</span>
        <div>
          <strong>${escapeHtml(item.title)}</strong>
          <p>${escapeHtml(item.text)}</p>
        </div>
      </div>
      `).join("")}
    </div>
  `;
  document.querySelector("#prompts-input-status").textContent = promptsReady
    ? promptDetails.text
    : "必需：粘贴 prompt 文本，或上传 prompts.json。";
  document.querySelector("#create-draft-btn").disabled = !ready;
}

function renderDraftPreview(draft) {
  state.selectedDraft = draft || null;
  const body = document.querySelector("#draft-tasks-body");
  const submitButton = document.querySelector("#submit-draft-btn");
  const planButton = document.querySelector("#plan-draft-btn");
  const status = document.querySelector("#draft-status");
  if (!draft) {
    body.innerHTML = '<tr><td colspan="6">还没有批次预览。</td></tr>';
    submitButton.disabled = true;
    planButton.disabled = true;
    status.textContent = "还没有批次预览";
    status.className = "status-pill idle";
    state.lastRenderedDraftId = "";
    state.lastRenderedDraftUpdatedAt = "";
    updateDraftSelectionStatus();
    return;
  }
  submitButton.disabled = false;
  planButton.disabled = false;
  status.textContent = `${draft.task_count} 个任务`;
  status.className = "status-pill queued";
  body.innerHTML = draft.tasks.map((task) => {
    const drawText = Number(task.draw_count || 1) > 1 ? `<small class="draw-tag">第 ${escapeHtml(task.draw_index)} / ${escapeHtml(task.draw_count)} 遍</small>` : "";
    return `
      <tr>
        <td><input class="draft-task-checkbox" type="checkbox" value="${escapeHtml(task.task_id)}" checked></td>
        <td>${escapeHtml(task.order)}${drawText}</td>
        <td>${escapeHtml(task.seed_value)}</td>
        <td>${escapeHtml(task.expected_output_name)}</td>
        <td>${renderInputRefs(task.input_urls || [])}</td>
        <td class="prompt-cell">${escapeHtml(task.prompt_text)}</td>
      </tr>
    `;
  }).join("");
  const selectAll = document.querySelector("#draft-select-all");
  if (selectAll) {
    selectAll.checked = true;
    selectAll.indeterminate = false;
  }
  state.lastRenderedDraftId = draft.id;
  state.lastRenderedDraftUpdatedAt = draft.updated_at || "";
  updateDraftSelectionStatus();
}

function draftTaskCheckboxes() {
  return Array.from(document.querySelectorAll(".draft-task-checkbox"));
}

function selectedDraftTaskIds() {
  return draftTaskCheckboxes()
    .filter((checkbox) => checkbox.checked)
    .map((checkbox) => checkbox.value)
    .filter(Boolean);
}

function updateDraftSelectionStatus() {
  const status = document.querySelector("#draft-selection-status");
  const selectAll = document.querySelector("#draft-select-all");
  const scope = document.querySelector("#draft-submit-scope")?.value || "all";
  const boxes = draftTaskCheckboxes();
  const selected = selectedDraftTaskIds().length;
  if (selectAll) {
    selectAll.indeterminate = selected > 0 && selected < boxes.length;
    selectAll.checked = boxes.length > 0 && selected === boxes.length;
  }
  if (!status) {
    return;
  }
  status.textContent = scope === "selected"
    ? `将提交已勾选 ${selected}/${boxes.length} 条`
    : `将提交全部 ${boxes.length} 条`;
}

function draftSubmitPayload() {
  const scope = document.querySelector("#draft-submit-scope")?.value || "all";
  const payload = { runtime_overrides: runtimeOverrides() };
  if (scope !== "selected") {
    return payload;
  }
  const ids = selectedDraftTaskIds();
  if (!ids.length) {
    throw new Error("请至少勾选一条任务。");
  }
  payload.selected_task_ids = ids;
  return payload;
}

function taskStatusPill(status) {
  const normalized = String(status || "pending").toLowerCase();
  return `<span class="task-status ${escapeHtml(normalized)}">${escapeHtml(TASK_STATUS_LABELS[normalized] || normalized)}</span>`;
}

function isImageUrl(url) {
  return /\.(png|jpe?g|webp|bmp|gif)(\?|#|$)/i.test(String(url || ""));
}

function isVideoUrl(url) {
  return /\.(mp4|webm|mov|m4v|avi)(\?|#|$)/i.test(String(url || ""));
}

function previewableMediaKind(url) {
  if (isImageUrl(url)) {
    return "image";
  }
  if (isVideoUrl(url)) {
    return "video";
  }
  return "";
}

function openMediaPreview(url, title = "预览") {
  const modal = document.querySelector("#media-preview-modal");
  const body = document.querySelector("#media-preview-body");
  const heading = document.querySelector("#media-preview-title");
  const kind = previewableMediaKind(url);
  if (!modal || !body || !kind) {
    window.open(url, "_blank", "noreferrer");
    return;
  }
  heading.textContent = title || "预览";
  body.innerHTML = kind === "video"
    ? `<video src="${escapeHtml(url)}" controls autoplay playsinline></video>`
    : `<img src="${escapeHtml(url)}" alt="${escapeHtml(title || "preview")}">`;
  modal.classList.remove("hidden");
}

function closeMediaPreview() {
  const modal = document.querySelector("#media-preview-modal");
  const body = document.querySelector("#media-preview-body");
  if (body) {
    body.innerHTML = "";
  }
  modal?.classList.add("hidden");
}

function userIsPreviewingMedia() {
  const modalOpen = !document.querySelector("#media-preview-modal")?.classList.contains("hidden");
  if (modalOpen) {
    return true;
  }
  return Array.from(document.querySelectorAll(".expanded-detail-row video")).some((video) => {
    return !video.paused || (video.currentTime > 0 && !video.ended);
  });
}

function renderInputRefs(inputUrls = []) {
  const uniqueRefs = [];
  const seen = new Set();
  for (const ref of inputUrls || []) {
    const key = ref.url || ref.path || `${ref.kind || ""}:${ref.label || ""}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    uniqueRefs.push(ref);
  }
  if (!uniqueRefs.length) {
    return "-";
  }
  return `
    <div class="input-preview-list">
      ${uniqueRefs.map((ref) => {
        const url = withAccessToken(ref.url || "");
        const label = ref.label || ref.kind || "input";
        const image = isImageUrl(ref.url)
          ? `<img src="${escapeHtml(url)}" alt="${escapeHtml(label)}" loading="lazy">`
          : '<span class="material-symbols-outlined">image</span>';
        const previewAttrs = previewableMediaKind(ref.url)
          ? `data-preview-url="${escapeHtml(url)}" data-preview-title="${escapeHtml(label)}"`
          : "";
        return `
          <a class="input-preview-card" href="${escapeHtml(url)}" target="_blank" rel="noreferrer" title="${escapeHtml(label)}" ${previewAttrs}>
            ${image}
            <small>${escapeHtml(label)}</small>
          </a>
        `;
      }).join("")}
    </div>
  `;
}

function renderVideoPreviews(run) {
  const completedTasks = (run.tasks || []).filter((task) => task.status === "completed" && task.output_url);
  if (!completedTasks.length) {
    return "";
  }
  return `
    <section class="video-preview-section">
      <div class="detail-section-title">
        <strong>完成视频预览</strong>
        <span>${completedTasks.length} 个可播放文件</span>
      </div>
      <div class="video-preview-grid">
        ${completedTasks.map((task) => {
          const url = withAccessToken(task.output_url);
          const title = task.expected_output_name || task.output_path || `task_${task.order}`;
          return `
            <article class="video-preview-card">
              <video src="${escapeHtml(url)}" controls preload="metadata"></video>
              <div class="video-preview-meta">
                <span>#${escapeHtml(task.order)} · ${escapeHtml(title)}</span>
                <button type="button" class="secondary-button compact-button" data-preview-url="${escapeHtml(url)}" data-preview-title="${escapeHtml(title)}">页面内预览</button>
              </div>
            </article>
          `;
        }).join("")}
      </div>
    </section>
  `;
}

function runActionButtons(run) {
  const buttons = [];
  if (ACTIVE_RUN_STATUSES.has(run.status)) {
    buttons.push(`<button data-run-action="stop" data-run-id="${escapeHtml(run.id)}" class="secondary-button">停止</button>`);
  }
  if (["completed", "failed", "stopped", "interrupted"].includes(run.status)) {
    buttons.push(`<button data-run-action="retry" data-run-id="${escapeHtml(run.id)}" class="secondary-button">重试</button>`);
  }
  const inspectText = state.selectedRunId === run.id ? "收起" : "详情";
  buttons.push(`<button data-run-action="inspect" data-run-id="${escapeHtml(run.id)}" class="secondary-button">${inspectText}</button>`);
  return buttons.join(" ");
}

function batchActionButtons(batch) {
  const id = escapeHtml(batch.id);
  return [
    `<button data-batch-action="run" data-batch-id="${id}" class="secondary-button">启动</button>`,
    `<button data-batch-action="schedule" data-batch-id="${id}" class="secondary-button">预约</button>`,
    `<button data-batch-action="inspect" data-batch-id="${id}" class="secondary-button">${state.selectedBatchId === batch.id ? "收起" : "详情"}</button>`,
    `<button data-batch-action="delete" data-batch-id="${id}" class="danger-button">删除</button>`,
  ].join(" ");
}

function renderRunProgress(run) {
  const summary = taskSummary(run);
  const doneCount = summary.completed + summary.failed + summary.cancelled + summary.unknown + summary.interrupted;
  const completePercent = percent(doneCount, summary.total);
  const parts = [
    `总数 ${summary.total}`,
    `已提交 ComfyUI ${summary.submitted}/${summary.total}`,
    `完成 ${summary.completed}`,
  ];
  if (summary.running) {
    parts.push(`运行 ${summary.running}`);
  }
  if (summary.queued) {
    parts.push(`排队 ${summary.queued}`);
  }
  if (summary.failed) {
    parts.push(`失败 ${summary.failed}`);
  }
  if (summary.interrupted) {
    parts.push(`中断 ${summary.interrupted}`);
  }
  if (run.local_queue_label) {
    parts.unshift(run.local_queue_label);
  }
  if (run.status === "queued" && summary.submitted === 0) {
    parts.push("等待提交到 ComfyUI");
  }
  return `
    <div class="run-progress">
      <div class="progress-bar"><span style="width: ${completePercent}%"></span></div>
      <div>${escapeHtml(parts.join(" | "))}</div>
      <small>当前任务：${summary.current_order ?? "-"}</small>
    </div>
  `;
}

function orderedActiveRuns(runs, queueState) {
  const byId = new Map(runs.map((run) => [run.id, run]));
  const ordered = [];
  const seen = new Set();
  const current = queueState?.current;
  if (current?.run_id && byId.has(current.run_id)) {
    const run = byId.get(current.run_id);
    ordered.push({ ...run, local_queue_label: "当前处理" });
    seen.add(run.id);
  }
  (queueState?.queued || []).forEach((item, index) => {
    if (!item?.run_id || !byId.has(item.run_id) || seen.has(item.run_id)) {
      return;
    }
    const run = byId.get(item.run_id);
    ordered.push({ ...run, local_queue_label: `本地提交队列 #${index + 1}` });
    seen.add(run.id);
  });
  runs
    .filter((run) => ACTIVE_RUN_STATUSES.has(run.status) && !seen.has(run.id))
    .forEach((run) => ordered.push(run));
  return ordered;
}

function renderRunRows(runs, targetSelector, emptyText) {
  const body = document.querySelector(targetSelector);
  if (!runs.length) {
    body.innerHTML = `<tr><td colspan="6">${escapeHtml(emptyText)}</td></tr>`;
    return;
  }
  body.innerHTML = runs.map((run) => {
    const detailRow = state.selectedRunId === run.id
      ? `<tr class="expanded-detail-row"><td colspan="6">${renderRunDetailContent(run)}</td></tr>`
      : "";
    return `
      <tr class="${state.selectedRunId === run.id ? "row-expanded" : ""}">
        <td class="mono-cell" title="${escapeHtml(run.id)}">${escapeHtml(run.id)}</td>
        <td><span class="status-pill ${escapeHtml(run.status)}">${escapeHtml(RUN_STATUS_LABELS[run.status] || run.status)}</span></td>
        <td class="mono-cell batch-info-cell" title="${escapeHtml(run.batch_id)}">
          ${escapeHtml(run.batch_id)}
          <small>${escapeHtml(batchMetaLine(run))}</small>
        </td>
        <td>${escapeHtml(run.created_at || "-")}</td>
        <td>${renderRunProgress(run)}</td>
        <td>${runActionButtons(run)}</td>
      </tr>
      ${detailRow}
    `;
  }).join("");
}

function renderPlannedBatches(batches) {
  const planned = batches.filter(isPlanBatch);
  const body = document.querySelector("#planned-batches-body");
  if (!planned.length) {
    body.innerHTML = '<tr><td colspan="6">计划箱为空。生成批次预览后点“保存到计划箱”，就会出现在这里。</td></tr>';
    return;
  }
  body.innerHTML = planned.map((batch) => {
    const schedule = batch.schedule?.run_at ? `${batch.status === "scheduled" ? "已预约" : "计划"} ${batch.schedule.run_at}` : "未预约";
    const detailRow = state.selectedBatchId === batch.id
      ? `<tr class="expanded-detail-row"><td colspan="6">${renderBatchDetailContent(batch)}</td></tr>`
      : "";
    return `
      <tr class="${state.selectedBatchId === batch.id ? "row-expanded" : ""}">
        <td class="mono-cell batch-info-cell" title="${escapeHtml(batch.id)}">
          ${escapeHtml(batch.id)}
          <small>${escapeHtml(batch.source_kind || "-")}</small>
          <small>${escapeHtml(batchMetaLine(batch))}</small>
        </td>
        <td><span class="status-pill ${escapeHtml(batch.status || "planned")}">${escapeHtml(batch.status === "scheduled" ? "已预约" : "已暂存")}</span></td>
        <td>${escapeHtml(batch.created_at || "-")}</td>
        <td>${escapeHtml(batch.task_count || 0)}</td>
        <td>${escapeHtml(schedule)}</td>
        <td>${batchActionButtons(batch)}</td>
      </tr>
      ${detailRow}
    `;
  }).join("");
}

function updateQueueMetrics(runs, batches, queueState) {
  const activeRuns = runs.filter((run) => ACTIVE_RUN_STATUSES.has(run.status));
  const planned = batches.filter(isPlanBatch);
  const completed = runs.filter((run) => run.status === "completed");
  const failed = runs.filter((run) => ["failed", "unknown", "interrupted"].includes(run.status));
  const queuedCount = queueState?.queued?.length || 0;
  const metrics = {
    "#queue-active-count": activeRuns.length + queuedCount,
    "#queue-planned-count": planned.length,
    "#queue-completed-count": completed.length,
    "#queue-failed-count": failed.length,
  };
  for (const [selector, value] of Object.entries(metrics)) {
    const node = document.querySelector(selector);
    if (node) {
      node.textContent = String(value);
    }
  }
}

function renderRuns(runs, queueState) {
  const current = queueState?.current;
  const queued = queueState?.queued || [];
  const currentRun = current ? runs.find((run) => run.id === current.run_id) : null;
  const currentSummary = taskSummary(currentRun);
  const currentText = currentRun
    ? `${currentRun.id}，ComfyUI ${currentSummary.submitted}/${currentSummary.total}`
    : "无";
  document.querySelector("#queue-summary").textContent = `当前运行：${currentText} | 待运行批次：${queued.length}`;

  const activeRuns = orderedActiveRuns(runs, queueState);
  const historyRuns = runs.filter((run) => !ACTIVE_RUN_STATUSES.has(run.status));
  renderRunRows(activeRuns, "#active-runs-body", "当前没有正在运行或等待提交的 Run。");
  renderRunRows(historyRuns, "#runs-body", "还没有历史运行记录。");
  updateQueueMetrics(runs, currentBatches(), queueState);
}

function renderRunDetailContent(run) {
  if (!run) {
    return '<div class="detail-content">没有找到这条运行记录。</div>';
  }
  const summary = taskSummary(run);
  const rows = (run.tasks || []).map((task) => {
    const promptId = task.prompt_id || "-";
    const outputUrl = task.output_url ? withAccessToken(task.output_url) : "";
    const outputTitle = task.expected_output_name || task.output_path || "输出文件";
    const output = task.output_url
      ? `<button type="button" class="link-button" data-preview-url="${escapeHtml(outputUrl)}" data-preview-title="${escapeHtml(outputTitle)}">${escapeHtml(outputTitle)}</button>`
      : escapeHtml(task.expected_output_name || "-");
    const finalPath = task.final_output_path ? `<small>${escapeHtml(task.final_output_path)}</small>` : "";
    const retryButton = `<button type="button" class="secondary-button compact-button" data-run-action="retry-task" data-run-id="${escapeHtml(run.id)}" data-task-id="${escapeHtml(task.task_id || "")}">单条再跑</button>`;
    return `
      <tr>
        <td>${escapeHtml(task.order)}</td>
        <td>${taskStatusPill(task.status)}</td>
        <td class="mono-cell" title="${escapeHtml(promptId)}">${escapeHtml(promptId)}</td>
        <td>${escapeHtml(task.submitted_at || "-")}</td>
        <td>${escapeHtml(task.started_at || "-")}</td>
        <td>${escapeHtml(task.finished_at || "-")}</td>
        <td>${escapeHtml(formatSeconds(task.duration_seconds))}</td>
        <td>${renderInputRefs(task.input_urls || [])}</td>
        <td>${output}${finalPath}</td>
        <td>${escapeHtml(task.error || "")}</td>
        <td>${retryButton}</td>
      </tr>
    `;
  }).join("");
  const logs = (run.logs || []).slice(-40).join("\n");
  return `
    <div class="detail-content">
      <div class="detail-head">
        <div>
          <strong>${escapeHtml(run.id)}</strong>
          <p>${escapeHtml(RUN_STATUS_LABELS[run.status] || run.status)} | 批次 ${escapeHtml(run.batch_id)}</p>
        </div>
        <div class="detail-stats">
          <span class="status-pill idle">总任务 ${summary.total}</span>
          <span class="status-pill queued">已提交 ${summary.submitted}/${summary.total}</span>
          <span class="status-pill completed">完成 ${summary.completed}</span>
          <span class="status-pill failed">失败 ${summary.failed}</span>
          <span class="status-pill interrupted">中断 ${summary.interrupted}</span>
        </div>
      </div>
      ${renderVideoPreviews(run)}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>状态</th>
              <th>prompt_id</th>
              <th>提交时间</th>
              <th>开始等待</th>
              <th>结束时间</th>
              <th>生成用时</th>
              <th>输入素材</th>
              <th>输出</th>
              <th>错误</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>${rows || '<tr><td colspan="11">没有任务明细。</td></tr>'}</tbody>
        </table>
      </div>
      <pre class="logs-box">${escapeHtml(logs || "暂无日志。")}</pre>
    </div>
  `;
}

function renderBatchDetailContent(batch) {
  if (!batch) {
    return '<div class="detail-content">没有找到这个计划批次。</div>';
  }
  const rows = (batch.tasks || []).map((task) => {
    return `
      <tr>
        <td>${escapeHtml(task.order)}</td>
        <td>${escapeHtml(task.seed_value)}</td>
        <td>${escapeHtml(task.expected_output_name)}</td>
        <td>${renderInputRefs(task.input_urls || [])}</td>
        <td class="prompt-cell">${escapeHtml(task.prompt_text)}</td>
      </tr>
    `;
  }).join("");
  return `
    <div class="detail-content">
      <div class="detail-head">
        <div>
          <strong>${escapeHtml(batch.id)}</strong>
          <p>${escapeHtml(batch.status || "planned")} | ${escapeHtml(batch.source_kind || "-")} | ${escapeHtml(batch.task_count || 0)} 个任务</p>
          <p>${escapeHtml(batchMetaLine(batch))}</p>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Seed</th>
              <th>输出文件</th>
              <th>输入素材</th>
              <th>Prompt</th>
            </tr>
          </thead>
          <tbody>${rows || '<tr><td colspan="5">没有任务。</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  `;
}

function renderAccessInfo(info) {
  const box = document.querySelector("#access-info-box");
  const lanLinks = (info.lan_urls || [])
    .map((url) => `<a href="${escapeHtml(withAccessToken(url))}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>`)
    .join("<br>");
  box.innerHTML = `
    <strong>访问方式</strong>
    <p>本机：<a href="${escapeHtml(withAccessToken(info.local_url))}" target="_blank" rel="noreferrer">${escapeHtml(info.local_url)}</a></p>
    <p>局域网：${lanLinks || "未检测到局域网地址，或当前没有用 --public 启动。"}</p>
    <p>访问令牌：${info.token_enabled ? "已开启" : "未开启"}</p>
    <p>外网访问建议使用 Tailscale / Cloudflare Tunnel / 路由器端口转发，并开启访问令牌。</p>
  `;
}

async function toggleAccessInfo() {
  const box = document.querySelector("#access-info-box");
  if (!box.classList.contains("hidden")) {
    box.classList.add("hidden");
    return;
  }
  if (!state.accessInfo) {
    state.accessInfo = await api("/api/access-info");
  }
  renderAccessInfo(state.accessInfo);
  box.classList.remove("hidden");
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  state.dashboard = data;
  const runs = data.selected_project?.runs || [];
  const batches = data.selected_project?.batches || [];
  setQueueBadge(data.queue_state, runs);
  renderProfiles(data.selected_project?.profiles || []);
  if (!userIsPreviewingMedia()) {
    renderRuns(runs, data.queue_state);
    renderPlannedBatches(batches);
  } else {
    updateQueueMetrics(runs, batches, data.queue_state);
  }
  loadRuntimeProfileStatus().catch(console.error);
  if (!state.settingsHydrated) {
    hydrateSettings(data.selected_project);
    state.settingsHydrated = true;
  }
  renderModeFields();
  if (state.selectedDraft) {
    const freshDraft = (data.selected_project?.drafts || []).find((draft) => draft.id === state.selectedDraft.id);
    const nextDraft = freshDraft || state.selectedDraft;
    if (
      state.lastRenderedDraftId !== nextDraft.id
      || state.lastRenderedDraftUpdatedAt !== (nextDraft.updated_at || "")
    ) {
      renderDraftPreview(nextDraft);
    }
  } else {
    renderDraftPreview(null);
  }
}

async function handleProfileUpload(event) {
  event.preventDefault();
  await saveSettings();
  const project = currentProject();
  const file = fileList("#workflow-file")[0];
  if (!project) {
    throw new Error("本地项目尚未初始化。");
  }
  if (!file) {
    throw new Error("请先选择 workflow JSON 文件。");
  }
  const form = new FormData();
  form.append("file", file);
  form.append("name", document.querySelector("#profile-name").value.trim() || file.name.replace(/\.json$/i, ""));
  const configHint = workflowConfigHint();
  if (Object.keys(configHint).length) {
    form.append("config_hint_json", JSON.stringify(configHint));
  }
  await api(`/api/projects/${project.id}/profiles/upload`, { method: "POST", body: form });
  await loadDashboard();
}

async function readPromptsInput(form) {
  const text = document.querySelector("#prompts-text").value.trim();
  const file = fileList("#prompts-file")[0];
  if (text) {
    form.append("prompts_text", text);
  }
  if (file) {
    form.append("prompts_file", file);
  }
}

function appendFiles(form, name, files) {
  for (const file of files) {
    form.append(name, file);
  }
}

async function handleDraftCreate(event) {
  event.preventDefault();
  await saveSettings();
  const project = currentProject();
  const profileId = document.querySelector("#draft-profile-id").value;
  if (!project || !profileId) {
    throw new Error("请先选择或上传 workflow。");
  }

  const promptDetails = promptInputDetails();
  const info = modeInfo();
  if (!promptDetails.ready || promptDetails.error) {
    throw new Error(promptDetails.text);
  }
  if (info.expectedTasks !== null && promptDetails.count !== null && promptDetails.count !== info.expectedTasks) {
    throw new Error(`素材会生成 ${info.expectedTasks} 个任务，但 prompts 是 ${promptDetails.count} 条。`);
  }

  const form = new FormData();
  form.append("profile_id", profileId);
  form.append("runtime_overrides_json", JSON.stringify(runtimeOverrides({ includeDraftMode: true })));
  await readPromptsInput(form);

  const mode = document.querySelector("#draft-mode").value;
  let endpoint = `/api/projects/${project.id}/drafts/prompt-only`;
  if (mode === "i2v_storyboard") {
    endpoint = `/api/projects/${project.id}/drafts/storyboard`;
    form.append("storyboard_file", fileList("#storyboard-file")[0]);
    form.append("rows", document.querySelector("#rows").value);
    form.append("cols", document.querySelector("#cols").value);
    form.append("cell_count", document.querySelector("#storyboard-cell-count").value);
    form.append("margin", document.querySelector("#margin").value);
    form.append("gutter", document.querySelector("#gutter").value);
  } else if (mode === "i2v_first_batch") {
    endpoint = `/api/projects/${project.id}/drafts/image-batch`;
    appendFiles(form, "image_files", fileList("#first-image-files"));
  } else if (mode === "i2v_first_last_batch") {
    endpoint = `/api/projects/${project.id}/drafts/first-last`;
    appendFiles(form, "first_files", fileList("#first-frame-files"));
    appendFiles(form, "last_files", fileList("#last-frame-files"));
    form.append("continuous_pairs", "false");
  } else if (mode === "i2v_first_last_continuous") {
    endpoint = `/api/projects/${project.id}/drafts/first-last`;
    appendFiles(form, "first_files", fileList("#first-frame-files"));
    form.append("continuous_pairs", "true");
  }

  const data = await api(endpoint, { method: "POST", body: form });
  renderDraftPreview(data.draft);
  await loadDashboard();
}

async function handleDraftSubmit() {
  const project = currentProject();
  if (!project || !state.selectedDraft) {
    throw new Error("请先生成批次预览。");
  }
  const payload = draftSubmitPayload();
  await api(`/api/projects/${project.id}/drafts/${state.selectedDraft.id}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.selectedDraft = null;
  await loadDashboard();
  setActiveTab("queue");
  setQueueView("dashboard");
}

async function handleDraftPlan() {
  const project = currentProject();
  if (!project || !state.selectedDraft) {
    throw new Error("请先生成批次预览。");
  }
  const payload = draftSubmitPayload();
  await api(`/api/projects/${project.id}/drafts/${state.selectedDraft.id}/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.selectedDraft = null;
  await loadDashboard();
  setActiveTab("queue");
  setQueueView("planbox");
}

async function handleRunAction(event) {
  const button = event.target.closest("button[data-run-action]");
  if (!button) {
    return;
  }
  const project = currentProject();
  const runId = button.dataset.runId;
  const action = button.dataset.runAction;
  const taskId = button.dataset.taskId;
  if (!project || !runId) {
    return;
  }
  if (action === "stop") {
    await api(`/api/projects/${project.id}/runs/${runId}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await loadDashboard();
  } else if (action === "retry") {
    await api(`/api/projects/${project.id}/runs/${runId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await loadDashboard();
  } else if (action === "retry-task") {
    if (!taskId) {
      throw new Error("找不到要重跑的任务 ID。");
    }
    await api(`/api/projects/${project.id}/runs/${runId}/tasks/${taskId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await loadDashboard();
  } else if (action === "inspect") {
    state.selectedRunId = state.selectedRunId === runId ? "" : runId;
    state.selectedBatchId = "";
    renderRuns(currentRuns(), state.dashboard?.queue_state);
  }
}

async function handleBatchAction(event) {
  const button = event.target.closest("button[data-batch-action]");
  if (!button) {
    return;
  }
  const project = currentProject();
  const batchId = button.dataset.batchId;
  const action = button.dataset.batchAction;
  if (!project || !batchId) {
    return;
  }
  if (action === "inspect") {
    state.selectedBatchId = state.selectedBatchId === batchId ? "" : batchId;
    state.selectedRunId = "";
    renderPlannedBatches(currentBatches());
    return;
  }
  if (action === "run") {
    await api(`/api/projects/${project.id}/batches/${batchId}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await loadDashboard();
    return;
  }
  if (action === "schedule") {
    const value = window.prompt("请输入预约启动时间，例如 2026-05-23 23:30：");
    if (!value) {
      return;
    }
    await api(`/api/projects/${project.id}/batches/${batchId}/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_at: value.trim() }),
    });
    await loadDashboard();
    return;
  }
  if (action === "delete") {
    if (!window.confirm("确定删除这个计划批次吗？只会清理 Planbox 中的暂存批次，不会影响已经提交的运行队列。")) {
      return;
    }
    await api(`/api/projects/${project.id}/batches/${batchId}`, { method: "DELETE" });
    await loadDashboard();
  }
}

function updateWorkflowFileStatus() {
  const file = selectedFileName("#workflow-file");
  document.querySelector("#workflow-file-status").textContent = file
    ? `已选择 workflow：${file}`
    : "选择从 ComfyUI 导出的 API workflow JSON。上传后会保存为可复用 Profile。";
}

function bindEvents() {
  document.querySelector("#refresh-btn").addEventListener("click", loadDashboard);
  document.querySelector("#access-info-btn").addEventListener("click", async () => {
    try {
      await toggleAccessInfo();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelectorAll(".top-tab").forEach((button) => {
    button.addEventListener("click", () => setActiveTab(button.dataset.tab));
  });
  document.querySelectorAll("[data-queue-view]").forEach((button) => {
    button.addEventListener("click", () => setQueueView(button.dataset.queueView));
  });
  document.querySelectorAll(".mode-pill").forEach((button) => {
    button.addEventListener("click", () => setActiveMode(button.dataset.mode));
  });
  document.querySelector("#view-prompt-formats-btn").addEventListener("click", openPromptFormats);
  document.querySelector("#workflow-search").addEventListener("input", () => renderProfiles(currentProfiles()));
  document.querySelector("#apply-comfyui-runtime-btn").addEventListener("click", async () => {
    try {
      await applyRuntimeProfile(false);
      toast("ComfyUI Docker 模式已切换。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#diagnose-comfyui-btn").addEventListener("click", async () => {
    try {
      await diagnoseComfyUIRuntime();
    } catch (error) {
      toast(error.message);
    }
  });

  document.querySelector("#profile-form").addEventListener("submit", async (event) => {
    try {
      await handleProfileUpload(event);
      toast("Workflow Profile 已保存。");
    } catch (error) {
      event.preventDefault();
      toast(error.message);
    }
  });
  document.querySelector("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await saveSettings();
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#draft-form").addEventListener("submit", async (event) => {
    try {
      await handleDraftCreate(event);
      toast("批次预览已生成。");
    } catch (error) {
      event.preventDefault();
      toast(error.message);
    }
  });
  document.querySelector("#plan-draft-btn").addEventListener("click", async () => {
    try {
      await handleDraftPlan();
      toast("已保存到计划箱。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#submit-draft-btn").addEventListener("click", async () => {
    try {
      await handleDraftSubmit();
      toast("已提交到运行队列。");
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#draft-tasks-body").addEventListener("change", (event) => {
    if (event.target.closest(".draft-task-checkbox")) {
      updateDraftSelectionStatus();
    }
  });
  document.querySelector("#draft-select-all").addEventListener("change", (event) => {
    draftTaskCheckboxes().forEach((checkbox) => {
      checkbox.checked = event.target.checked;
    });
    updateDraftSelectionStatus();
  });
  document.querySelector("#draft-submit-scope").addEventListener("change", updateDraftSelectionStatus);
  document.addEventListener("click", (event) => {
    const promptFormatCopy = event.target.closest("[data-prompt-format-copy]");
    if (promptFormatCopy) {
      const index = Number.parseInt(promptFormatCopy.dataset.promptFormatCopy || "", 10);
      const format = PROMPT_JSON_FORMATS[index];
      const status = document.querySelector(`[data-prompt-format-status="${index}"]`);
      if (format) {
        copyText(promptFormatText(format))
          .then(() => {
            status.textContent = "已复制";
            window.setTimeout(() => {
              status.textContent = "";
            }, 1800);
          })
          .catch((error) => {
            status.textContent = `复制失败：${error.message}`;
          });
      }
      return;
    }
    if (event.target.closest("[data-prompt-formats-close]")) {
      closePromptFormats();
      return;
    }
    const previewTarget = event.target.closest("[data-preview-url]");
    if (previewTarget) {
      event.preventDefault();
      openMediaPreview(previewTarget.dataset.previewUrl, previewTarget.dataset.previewTitle || previewTarget.textContent.trim());
      return;
    }
    if (event.target.closest("[data-preview-close]")) {
      closeMediaPreview();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePromptFormats();
      closeMediaPreview();
    }
  });
  document.querySelector("#stop-all-btn").addEventListener("click", async () => {
    try {
      await api("/api/runs/stop-all", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      await loadDashboard();
    } catch (error) {
      toast(error.message);
    }
  });

  document.querySelector("#active-runs-body").addEventListener("click", async (event) => {
    try {
      await handleRunAction(event);
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#runs-body").addEventListener("click", async (event) => {
    try {
      await handleRunAction(event);
    } catch (error) {
      toast(error.message);
    }
  });
  document.querySelector("#planned-batches-body").addEventListener("click", async (event) => {
    try {
      await handleBatchAction(event);
    } catch (error) {
      toast(error.message);
    }
  });

  document.querySelector("#workflow-file").addEventListener("change", updateWorkflowFileStatus);
  document.querySelector("#draft-profile-id").addEventListener("change", () => {
    renderDurationOptions(selectedProfile());
    renderUploadChecklist();
  });
  document.querySelector("#prompts-text").addEventListener("input", renderUploadChecklist);
  document.querySelector("#prompts-file").addEventListener("change", updatePromptFileInfo);
  document.querySelector("#seed-mode").addEventListener("change", updateSeedFieldVisibility);
  document.querySelector("#size-preset").addEventListener("change", applySizePreset);
  document.querySelector("#width-pixels").addEventListener("input", syncSizePreset);
  document.querySelector("#height-pixels").addEventListener("input", syncSizePreset);

  for (const selector of [
    "#storyboard-file",
    "#first-image-files",
    "#first-frame-files",
    "#last-frame-files",
    "#rows",
    "#cols",
    "#storyboard-cell-count",
  ]) {
    document.querySelector(selector).addEventListener("change", renderUploadChecklist);
    document.querySelector(selector).addEventListener("input", renderUploadChecklist);
  }
}

function startPolling() {
  if (state.polling) {
    window.clearInterval(state.polling);
  }
  state.polling = window.setInterval(() => {
    loadDashboard().catch(console.error);
  }, 4000);
}

async function main() {
  initAccessToken();
  bindEvents();
  setActiveTab(window.localStorage.getItem("batchStudioActiveTab") || "submit");
  setQueueView(window.localStorage.getItem("batchStudioQueueView") || "dashboard");
  setActiveMode(window.localStorage.getItem("batchStudioDraftMode") || "t2v");
  await loadDashboard();
  startPolling();
}

main().catch((error) => {
  console.error(error);
  toast(error.message);
});
