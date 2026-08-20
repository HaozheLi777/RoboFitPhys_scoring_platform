const state = {
  subjects: [], selected: null, metadata: null, preview: null,
  position: 0, mode: "raw", renderToken: 0, subjectToken: 0,
  renderTimer: null, pollTimer: null, playing: false,
  playStartedAt: 0, playStartPosition: 0, animationFrame: null,
  sets: [], drafts: {}, savingCard: null, annotationView: false,
  scrub: null, lastTickAt: null,
};
const $ = (selector) => document.querySelector(selector);
const elements = {
  subjectList: $("#subjectList"), subjectSearch: $("#subjectSearch"), refreshButton: $("#refreshButton"),
  dataRootInput: $("#dataRootInput"), dataRootOptions: $("#dataRootOptions"),
  applyDataRootButton: $("#applyDataRootButton"),
  subjectSection: $("#subjectSection"), annotationPanel: $("#annotationPanel"),
  annotationList: $("#annotationList"), annotationSubject: $("#annotationSubject"),
  exitAnnotationButton: $("#exitAnnotationButton"),
  archiveCount: $("#archiveCount"), currentSubject: $("#currentSubject"), emptyState: $("#emptyState"),
  reviewArea: $("#reviewArea"), cam1Image: $("#cam1Image"), cam2Image: $("#cam2Image"),
  cam1Video: $("#cam1Video"), cam2Video: $("#cam2Video"), cam1Frame: $("#cam1Frame"), cam2Frame: $("#cam2Frame"),
  cam1Format: $("#cam1Format"), cam2Format: $("#cam2Format"),
  timeline: $("#timeline"), currentTime: $("#currentTime"), totalTime: $("#totalTime"),
  progressLabel: $("#progressLabel"), framePosition: $("#framePosition"), playButton: $("#playButton"),
  playIcon: $("#playIcon"), stepBack: $("#stepBack"), stepForward: $("#stepForward"), toast: $("#toast"),
  rawModeButton: $("#rawModeButton"), videoModeButton: $("#videoModeButton"),
  previewStatusDot: $("#previewStatusDot"), previewStatusText: $("#previewStatusText"),
  previewStatusDetail: $("#previewStatusDetail"), generationProgress: $("#generationProgress"),
  generationProgressBar: $("#generationProgressBar"), generatePreviewButton: $("#generatePreviewButton"),
  deletePreviewButton: $("#deletePreviewButton"),
};

function formatDate(date) {
  const parts = date.split("_");
  return parts.length === 3 ? `${parts[0]} / ${parts[1].padStart(2, "0")} / ${parts[2].padStart(2, "0")}` : date;
}
function formatTime(seconds) {
  const safe = Math.max(0, Number(seconds) || 0), minutes = Math.floor(safe / 60);
  return `${String(minutes).padStart(2, "0")}:${(safe - minutes * 60).toFixed(1).padStart(4, "0")}`;
}
function formatBytes(bytes) { return bytes ? `${(bytes / 1048576).toFixed(bytes > 1073741824 ? 0 : 1)} MB` : "0 MB"; }
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]
  ));
}
function showToast(message) {
  elements.toast.textContent = message; elements.toast.classList.add("show");
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3200);
}
async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}
function subjectBase() {
  return state.selected ? `/api/subjects/${encodeURIComponent(state.selected.date)}/${encodeURIComponent(state.selected.subject_id)}` : "";
}
function baseForSubject(subject) {
  return `/api/subjects/${encodeURIComponent(subject.date)}/${encodeURIComponent(subject.subject_id)}`;
}
function cardActions(item) {
  const preview = item.preview || { status: "absent", progress: 0 };
  const annotate = `<button class="card-action annotate" data-action="annotate" data-key="${item.key}" type="button" ${item.ready ? "" : "disabled"}>录入</button>`;
  if (preview.status === "ready") return `
    <span class="card-preview-state ready">● ${formatBytes(preview.cache_bytes)}</span>
    ${annotate}
    <button class="card-action use" data-action="use" data-key="${item.key}" type="button">使用视频</button>
    <button class="card-action delete" data-action="delete" data-key="${item.key}" type="button">删除</button>`;
  if (preview.status === "generating") return `
    <span class="card-preview-state generating"><i style="--card-progress:${preview.progress * 100}%"></i>生成 ${Math.round(preview.progress * 100)}%</span>
    ${annotate}
    <button class="card-action delete" data-action="delete" data-key="${item.key}" type="button">取消</button>`;
  return `
    <span class="card-preview-state">暂无视频</span>
    ${annotate}
    <button class="card-action generate" data-action="generate" data-key="${item.key}" type="button" ${preview.ffmpeg_available === false ? "disabled" : ""}>生成视频</button>`;
}

function renderSubjects() {
  const previousScroll = elements.subjectList.scrollTop;
  const filter = elements.subjectSearch.value.trim().toLowerCase();
  const visible = state.subjects.filter((item) => item.subject_id.toLowerCase().includes(filter));
  if (!visible.length) {
    elements.subjectList.innerHTML = `<div class="no-subjects">${state.subjects.length ? "没有匹配的被试编号" : "data 目录下暂无可预览被试"}</div>`;
    return;
  }
  const groups = visible.reduce((result, item) => {
    if (!result.has(item.date)) result.set(item.date, []);
    result.get(item.date).push(item); return result;
  }, new Map());
  elements.subjectList.innerHTML = Array.from(groups, ([date, subjects]) => `
    <div class="date-group"><p class="date-label">${formatDate(date)}</p>${subjects.map((item) => `
      <div class="subject-card ${state.selected?.key === item.key ? "active" : ""}">
        <button class="subject-select" data-key="${item.key}" type="button">
          <span class="subject-avatar">${item.subject_id.slice(0, 2)}</span>
          <span><strong>${item.subject_id}</strong><small>${item.camera_count} 路 RGB 数据 · ${item.ready ? "可预览" : "数据不全"}</small></span>
          <span class="chevron">›</span>
        </button>
        <div class="subject-card-actions">${cardActions(item)}</div>
      </div>`).join("")}</div>`).join("");
  elements.subjectList.scrollTop = previousScroll;
  elements.subjectList.querySelectorAll(".subject-select").forEach((button) => {
    button.addEventListener("click", () => selectSubject(state.subjects.find((item) => item.key === button.dataset.key)));
  });
  elements.subjectList.querySelectorAll(".card-action").forEach((button) => {
    button.addEventListener("click", () => handleCardAction(button.dataset.action, state.subjects.find((item) => item.key === button.dataset.key)));
  });
}

function updateSubjectPreview(subject, preview) {
  const item = state.subjects.find((candidate) => candidate.key === subject.key);
  if (item) item.preview = { ...item.preview, ...preview };
  renderSubjects();
}

async function handleCardAction(action, subject) {
  if (!subject) return;
  if (action === "generate") return generateForSubject(subject);
  if (action === "delete") return deleteForSubject(subject);
  if (action === "annotate") return enterAnnotation(subject);
  if (action === "use") {
    if (state.selected?.key !== subject.key) await selectSubject(subject);
    if (state.selected?.key === subject.key && state.preview?.status === "ready") activateVideoMode();
  }
}

async function generateForSubject(subject) {
  try {
    const preview = await requestJson(`${baseForSubject(subject)}/preview`, { method: "POST" });
    updateSubjectPreview(subject, preview);
    if (state.selected?.key === subject.key) applyPreviewStatus(preview);
    pollCardPreview(subject);
  } catch (error) { showToast(error.message); }
}

async function pollCardPreview(subject) {
  try {
    const preview = await requestJson(`${baseForSubject(subject)}/preview/status`);
    updateSubjectPreview(subject, preview);
    if (state.selected?.key === subject.key) applyPreviewStatus(preview);
    if (preview.status === "generating") setTimeout(() => pollCardPreview(subject), 800);
    else if (preview.status === "ready") showToast(`${subject.subject_id} 流畅预览已生成，可点击“使用视频”`);
  } catch (error) { showToast(error.message); }
}

async function deleteForSubject(subject) {
  const generating = subject.preview?.status === "generating";
  if (!confirm(generating ? `取消 ${subject.subject_id} 的视频生成？` : `删除 ${subject.subject_id} 的 MP4 缓存？原始 RAW 不会删除。`)) return;
  if (state.selected?.key === subject.key) {
    stopPlayback(); if (state.mode === "video") activateRawMode(); resetVideos();
  }
  try {
    const preview = await requestJson(`${baseForSubject(subject)}/preview`, { method: "DELETE" });
    updateSubjectPreview(subject, preview);
    if (state.selected?.key === subject.key) applyPreviewStatus(preview);
    showToast(generating ? "生成已取消，临时文件已清理" : "MP4 缓存已删除，原始数据未受影响");
  } catch (error) { showToast(error.message); }
}
async function loadDataRoot() {
  try {
    const data = await requestJson("/api/data-root");
    elements.dataRootInput.value = data.data_root;
    elements.dataRootOptions.innerHTML = data.candidates
      .map((candidate) => `<option value="${escapeHtml(candidate)}"></option>`).join("");
  } catch (error) { showToast(error.message); }
}
function clearSelection() {
  stopPlayback(); clearTimeout(state.pollTimer); ++state.subjectToken;
  state.selected = null; state.metadata = null; state.preview = null; state.position = 0;
  state.mode = "raw"; resetVideos(); setModeButtons();
  state.annotationView = false; updateSidebarView();
  resetSets(); renderSubjects();
  elements.currentSubject.textContent = "—";
  elements.emptyState.hidden = false; elements.reviewArea.hidden = true;
}
async function applyDataRoot() {
  const value = elements.dataRootInput.value.trim();
  if (!value) return showToast("请填写数据目录路径");
  try {
    const data = await requestJson("/api/data-root", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_root: value }),
    });
    elements.dataRootInput.value = data.data_root;
    elements.dataRootOptions.innerHTML = data.candidates
      .map((candidate) => `<option value="${escapeHtml(candidate)}"></option>`).join("");
    clearSelection();
    await loadSubjects();
    showToast(`数据目录已切换: ${data.data_root}`);
  } catch (error) { showToast(error.message); }
}
async function loadSubjects({ autoSelect = false } = {}) {
  elements.refreshButton.classList.add("refreshing");
  try {
    const data = await requestJson("/api/subjects"); state.subjects = data.subjects;
    elements.archiveCount.textContent = `${data.subjects.length} 位被试可用`; renderSubjects();
    if (autoSelect && data.subjects.length) await selectSubject(data.subjects[0]);
  } catch (error) {
    elements.subjectList.innerHTML = `<div class="no-subjects">读取失败<br>${error.message}</div>`;
    elements.archiveCount.textContent = "数据归档离线"; showToast(error.message);
  } finally { elements.refreshButton.classList.remove("refreshing"); }
}

function resetVideos() {
  [elements.cam1Video, elements.cam2Video].forEach((video) => {
    video.pause(); video.removeAttribute("src"); video.load(); video.hidden = true;
  });
  elements.cam1Image.hidden = false; elements.cam2Image.hidden = false;
  elements.cam1Format.textContent = "RGB / RAW"; elements.cam2Format.textContent = "RGB / RAW";
}
async function selectSubject(subject) {
  if (!subject?.ready) return showToast("该被试缺少两路 RGB 相机数据");
  stopPlayback(); clearTimeout(state.pollTimer); const token = ++state.subjectToken;
  state.selected = subject; state.metadata = null; state.preview = null; state.position = 0; state.mode = "raw";
  resetVideos(); setModeButtons(); renderSubjects(); elements.currentSubject.textContent = subject.subject_id;
  elements.emptyState.hidden = true; elements.reviewArea.hidden = false;
  state.annotationView = false; updateSidebarView();
  resetSets();
  document.querySelectorAll(".camera-card").forEach((card) => card.classList.add("loading"));
  try {
    state.metadata = await requestJson(subjectBase());
    if (token !== state.subjectToken) return;
    updateTimeline(0, { immediate: true });
    await loadSets();
    await refreshPreviewStatus({ autoActivate: false, token });
  } catch (error) {
    showToast(error.message); document.querySelectorAll(".camera-card").forEach((card) => card.classList.remove("loading"));
  }
}

function playbackDuration() {
  return state.mode === "video" && state.preview?.duration_sec ? Number(state.preview.duration_sec) : Number(state.metadata?.duration_sec) || 0;
}
function updateTimelineDisplay(position) {
  state.position = Math.max(0, Math.min(10000, Math.round(position))); const ratio = state.position / 10000;
  elements.timeline.value = state.position; elements.timeline.style.setProperty("--progress", `${ratio * 100}%`);
  elements.progressLabel.textContent = `${(ratio * 100).toFixed(1)}%`;
  elements.framePosition.textContent = String(state.position).padStart(5, "0");
  elements.currentTime.textContent = formatTime(playbackDuration() * ratio);
  elements.totalTime.textContent = ` / ${formatTime(playbackDuration())}`;
  state.metadata?.cameras.forEach((camera) => {
    const estimate = Math.round((camera.frame_count - 1) * ratio);
    elements[`${camera.camera_id}Frame`].textContent = `帧 ${estimate.toLocaleString("zh-CN")} / ${camera.frame_count.toLocaleString("zh-CN")}`;
  });
}
function updateTimeline(position, { immediate = false } = {}) {
  updateTimelineDisplay(position);
  if (state.mode === "video") {
    const target = playbackDuration() * state.position / 10000;
    [elements.cam1Video, elements.cam2Video].forEach((video) => {
      if (Number.isFinite(video.duration)) video.currentTime = Math.min(target, video.duration);
    });
  } else {
    clearTimeout(state.renderTimer);
    if (immediate) renderRawFrames(); else state.renderTimer = setTimeout(renderRawFrames, 65);
  }
}
function preloadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image(); image.onload = () => resolve(image.src); image.onerror = reject; image.src = url;
  });
}
async function renderRawFrames() {
  if (!state.selected || !state.metadata || state.mode !== "raw") return;
  const token = ++state.renderToken, position = state.position;
  document.querySelectorAll(".camera-card").forEach((card) => card.classList.add("loading"));
  try {
    const [cam1, cam2] = await Promise.all([
      preloadImage(`${subjectBase()}/frame/cam1?position=${position}`), preloadImage(`${subjectBase()}/frame/cam2?position=${position}`),
    ]);
    if (token !== state.renderToken || state.mode !== "raw") return;
    elements.cam1Image.src = cam1; elements.cam2Image.src = cam2;
  } catch (_) { if (token === state.renderToken) showToast("当前时间点的相机帧加载失败"); }
  finally { if (token === state.renderToken) document.querySelectorAll(".camera-card").forEach((card) => card.classList.remove("loading")); }
}

function setModeButtons() {
  elements.rawModeButton.classList.toggle("active", state.mode === "raw");
  elements.videoModeButton.classList.toggle("active", state.mode === "video");
  elements.videoModeButton.disabled = state.preview?.status !== "ready";
}
function activateRawMode() {
  if (state.mode === "raw") return;
  stopPlayback(); state.mode = "raw"; elements.cam1Video.hidden = true; elements.cam2Video.hidden = true;
  elements.cam1Image.hidden = false; elements.cam2Image.hidden = false; setModeButtons(); updateTimeline(state.position, { immediate: true });
  elements.cam1Format.textContent = "RGB / RAW"; elements.cam2Format.textContent = "RGB / RAW";
}
function activateVideoMode() {
  if (state.preview?.status !== "ready") return;
  stopPlayback(); state.mode = "video"; elements.cam1Image.hidden = true; elements.cam2Image.hidden = true;
  state.videoErrorShown = false;
  document.querySelectorAll(".camera-card").forEach((card) => card.classList.add("loading"));
  const stamp = Date.now();
  [[elements.cam1Video, "cam1"], [elements.cam2Video, "cam2"]].forEach(([video, camera]) => {
    const source = state.preview.videos[camera];
    if (!video.src || !video.src.includes(source)) { video.src = `${source}?v=${stamp}`; video.load(); }
    video.hidden = false;
  });
  elements.cam1Format.textContent = "H.264 / MP4"; elements.cam2Format.textContent = "H.264 / MP4";
  setModeButtons(); updateTimeline(state.position);
}

function applyPreviewStatus(preview, { autoActivate = false } = {}) {
  state.preview = preview; const status = preview.status;
  elements.previewStatusDot.className = `preview-status-dot ${status}`;
  elements.generationProgress.hidden = status !== "generating";
  elements.generationProgressBar.style.width = `${preview.progress * 100}%`;
  elements.generatePreviewButton.hidden = status === "ready";
  elements.generatePreviewButton.disabled = status === "generating";
  elements.generatePreviewButton.textContent = status === "generating" ? `生成中 ${Math.round(preview.progress * 100)}%` : status === "error" ? "重新生成" : "生成流畅预览";
  elements.deletePreviewButton.hidden = !["ready", "generating"].includes(status);
  elements.deletePreviewButton.textContent = status === "generating" ? "取消生成" : "删除缓存";
  elements.previewStatusText.textContent = status === "ready" ? "流畅预览已就绪" : status === "generating" ? preview.message : status === "error" ? "生成失败" : "尚未生成流畅预览";
  elements.previewStatusDetail.textContent = status === "ready" ? `${formatBytes(preview.cache_bytes)} · H.264 MP4 · 可随时删除`
    : status === "generating" ? `${preview.camera_id || "准备中"} · 已完成 ${Math.round(preview.progress * 100)}%`
      : status === "error" ? (preview.error || "请检查 FFmpeg 输出")
        : preview.ffmpeg_available ? "需要时生成临时 MP4，原始数据不受影响" : "当前环境未找到 FFmpeg";
  setModeButtons();
  if (status === "ready" && autoActivate) activateVideoMode();
  if (status !== "ready" && state.mode === "video") activateRawMode();
}
async function refreshPreviewStatus({ autoActivate = false, token = state.subjectToken } = {}) {
  if (!state.selected) return;
  try {
    const preview = await requestJson(`${subjectBase()}/preview/status`);
    if (token !== state.subjectToken) return;
    const item = state.subjects.find((subject) => subject.key === state.selected?.key);
    if (item) item.preview = { ...item.preview, ...preview };
    applyPreviewStatus(preview, { autoActivate });
    if (preview.status === "generating") state.pollTimer = setTimeout(() => refreshPreviewStatus({ autoActivate: true, token }), 800);
  } catch (error) { showToast(error.message); }
}
const WARMUP_LABELS = ["warm_up1", "warm_up2", "warm_up3"];
function isWarmupLabel(label) { return WARMUP_LABELS.includes(label); }
function draftTime(position) {
  // 与后端 _position_timestamp 同公式(公共时间轴的秒)。
  // 注意不能使用 playbackDuration():视频模式下 preview 时长与 subject 时长因 fps 取整有微小差异。
  return (Number(state.metadata?.duration_sec) || 0) * position / 10000;
}
function cardBoundaryDisplay(card, side) {
  const draftPosition = state.drafts[String(card.id)]?.[side];
  if (draftPosition != null) return formatTime(draftTime(draftPosition));
  const savedPosition = card[`${side}_position`];
  if (savedPosition != null) return formatTime(side === "start" ? card.start_sec : card.end_sec);
  return "—";
}
function cardDurationText(card) {
  const draft = state.drafts[String(card.id)];
  const startPosition = draft?.start != null ? draft.start : card.start_position;
  const endPosition = draft?.end != null ? draft.end : card.end_position;
  if (startPosition == null || endPosition == null) return "—";
  const startSec = draft?.start != null ? draftTime(draft.start) : card.start_sec;
  const endSec = draft?.end != null ? draftTime(draft.end) : card.end_sec;
  return formatTime(Math.max(0, endSec - startSec));
}
async function loadSets() {
  if (!state.selected) return;
  const token = state.subjectToken;
  try {
    const data = await requestJson(`${subjectBase()}/sets`);
    if (token !== state.subjectToken) return;
    state.sets = Array.isArray(data.sets) ? data.sets : [];
  } catch (error) {
    if (token !== state.subjectToken) return;
    showToast(error.message);
  }
  renderSetCards();
}
function resetSets() {
  state.sets = [];
  state.drafts = {};
  state.savingCard = null;
  renderSetCards();
}
function parseScoreInput(raw) {
  if (raw.trim() === "") return null;
  const score = Number(raw);
  if (!Number.isInteger(score) || score < 0 || score > 5) {
    showToast("评分需为 0 到 5 的整数");
    return undefined;
  }
  return score;
}
function updateSidebarView() {
  elements.subjectSection.hidden = state.annotationView;
  elements.annotationPanel.hidden = !state.annotationView;
  elements.annotationSubject.textContent = state.annotationView ? state.selected?.subject_id || "—" : "—";
}
async function enterAnnotation(subject) {
  if (!subject?.ready) return showToast("该被试缺少两路 RGB 相机数据");
  if (state.selected?.key !== subject.key) await selectSubject(subject);
  if (state.selected?.key !== subject.key) return;
  state.annotationView = true;
  updateSidebarView();
}
function exitAnnotation() {
  state.annotationView = false;
  updateSidebarView();
}
function renderSetCards() {
  // 固定序列顺序 = 卡片 id 升序(播种时 ids 1..11 即 warm_up1-3 + 表格顺序)
  const sorted = [...state.sets].sort((a, b) => Number(a.id) - Number(b.id));
  elements.annotationList.innerHTML = sorted.length ? sorted.map((set) => {
    const warmup = isWarmupLabel(set.label);
    const marked = set.start_position != null;
    return `
    <article class="set-card" data-set-id="${set.id}" data-status="${marked ? "marked" : "unmarked"}">
      <header class="set-card-head">
        <span class="set-card-index">${String(Number(set.id)).padStart(2, "0")}</span>
        <div class="set-card-title">
          <strong class="set-card-label">${escapeHtml(set.label)}</strong>
        </div>
        ${warmup ? `<span class="set-card-badge">热身</span>` : ""}
      </header>
      <div class="set-card-times">
        <span>开始 <strong>${cardBoundaryDisplay(set, "start")}</strong></span>
        <span>结束 <strong>${cardBoundaryDisplay(set, "end")}</strong></span>
        <span>时长 <strong>${cardDurationText(set)}</strong></span>
      </div>
      <div class="set-card-actions">
        <button class="card-btn start" data-card-start="${set.id}" type="button">Set Start</button>
        <button class="card-btn end" data-card-end="${set.id}" type="button">Set End</button>
      </div>
      <div class="set-card-actions">
        ${warmup ? "" : `
        <label class="set-card-score">评分
          <input type="number" min="0" max="5" step="1" data-card-score="${set.id}" value="${set.score == null ? "" : set.score}" placeholder="—" />
        </label>`}
        <button class="card-btn save" data-card-save="${set.id}" type="button">保存</button>
        <button class="card-btn clear" data-card-clear="${set.id}" type="button" ${marked ? "" : "hidden"}>清除</button>
      </div>
    </article>`;
  }).join("") : `<div class="sets-empty">未加载动作卡片</div>`;
  elements.annotationList.querySelectorAll(".set-card").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest("input, button")) return;
      const set = state.sets.find((candidate) => String(candidate.id) === card.dataset.setId);
      if (set?.start_position != null) updateTimeline(set.start_position, { immediate: true });
    });
    card.querySelectorAll("[data-card-start]").forEach((button) => {
      // 播放中标记不中断播放:直接取当前 position 打点
      button.addEventListener("click", () => captureCardBoundary(card.dataset.setId, "start"));
    });
    card.querySelectorAll("[data-card-end]").forEach((button) => {
      button.addEventListener("click", () => captureCardBoundary(card.dataset.setId, "end"));
    });
    card.querySelectorAll("[data-card-save]").forEach((button) => {
      button.addEventListener("click", () => saveCard(card.dataset.setId));
    });
    card.querySelectorAll("[data-card-clear]").forEach((button) => {
      button.addEventListener("click", () => clearCard(card.dataset.setId));
    });
  });
}
function captureCardBoundary(cardId, kind) {
  if (!state.selected || !state.metadata) return showToast("请先选择被试");
  const draft = state.drafts[String(cardId)] || { start: null, end: null };
  const position = state.position;
  if (kind === "start") {
    if (draft.end != null && position >= draft.end)
      return showToast("开始位置需早于结束位置,请重新标记");
    draft.start = position;
  } else {
    if (draft.start != null && position <= draft.start)
      return showToast("结束位置需晚于开始位置");
    draft.end = position;
  }
  state.drafts[String(cardId)] = draft;
  renderSetCards();
}
async function saveCard(cardId) {
  if (!state.selected) return;
  if (state.savingCard != null) return;
  const card = state.sets.find((set) => String(set.id) === String(cardId));
  if (!card) return;
  const draft = state.drafts[String(cardId)] || {};
  const start = draft.start != null ? draft.start : card.start_position;
  const end = draft.end != null ? draft.end : card.end_position;
  if (start == null || end == null) return showToast("请先标记开始与结束位置");
  if (start >= end) return showToast("结束位置需晚于开始位置");
  let score = null;
  if (!isWarmupLabel(card.label)) {
    const input = elements.annotationList.querySelector(`input[data-card-score="${cardId}"]`);
    score = parseScoreInput(input ? input.value : "");
    if (score === undefined) return;
  }
  state.savingCard = cardId;
  const subjectKey = state.selected.key;
  try {
    const data = await requestJson(`${subjectBase()}/sets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: card.label, start_position: start, end_position: end, score }),
    });
    if (state.selected?.key !== subjectKey) return;
    const index = state.sets.findIndex((set) => String(set.id) === String(cardId));
    if (index !== -1) state.sets[index] = data.set;
    delete state.drafts[String(cardId)];
    renderSetCards();
    showToast(`已保存 ${data.set.label}`);
  } catch (error) {
    if (state.selected?.key !== subjectKey) return;
    showToast(error.message);
  } finally {
    state.savingCard = null;
  }
}
async function clearCard(cardId) {
  if (!state.selected) return;
  const card = state.sets.find((set) => String(set.id) === String(cardId));
  if (!card) return;
  if (!confirm(`清除 ${card.label} 的分段与分数?卡片将保留,可重新标记。`)) return;
  const subjectKey = state.selected.key;
  try {
    const data = await requestJson(`${subjectBase()}/sets/${encodeURIComponent(cardId)}`, { method: "DELETE" });
    if (state.selected?.key !== subjectKey) return;
    const index = state.sets.findIndex((set) => String(set.id) === String(cardId));
    if (index !== -1) state.sets[index] = data.set;
    delete state.drafts[String(cardId)];
    renderSetCards();
    showToast(`已清除 ${card.label}`);
  } catch (error) {
    if (state.selected?.key !== subjectKey) return;
    showToast(error.message);
  }
}
async function generatePreview() {
  if (state.selected) generateForSubject(state.selected);
}
async function deletePreview() {
  if (state.selected) deleteForSubject(state.selected);
}

function stopPlayback() {
  state.playing = false; state.scrub = null; state.lastTickAt = null;
  elements.playIcon.textContent = "▶"; elements.playButton.setAttribute("aria-label", "播放");
  [elements.cam1Video, elements.cam2Video].forEach((video) => video.pause());
  if (state.animationFrame) cancelAnimationFrame(state.animationFrame); state.animationFrame = null;
}
// 按住左右方向键: 倍速快退/快进,不中断播放,松手恢复原状态
function startScrub(direction) {
  if (!state.selected || !state.metadata || state.scrub) return;
  state.scrub = { direction, wasPlaying: state.playing };
  state.lastTickAt = null;
  if (state.mode === "video") {
    // 快进快退期间暂停浏览器自身的推进,由 rAF 手动驱动 currentTime,方向与速率精确可控
    [elements.cam1Video, elements.cam2Video].forEach((video) => video.pause());
  }
  state.playing = true;
  elements.playIcon.textContent = "Ⅱ"; elements.playButton.setAttribute("aria-label", "暂停");
  state.playStartedAt = performance.now(); state.playStartPosition = state.position;
  state.animationFrame = requestAnimationFrame(state.mode === "video" ? videoPlaybackTick : rawPlaybackTick);
}
async function stopScrub(resume = true) {
  if (!state.scrub) return;
  const wasPlaying = state.scrub.wasPlaying;
  state.scrub = null; state.lastTickAt = null;
  if (state.mode === "video") {
    [elements.cam1Video, elements.cam2Video].forEach((video) => video.pause());
    if (wasPlaying && resume) {
      try {
        await Promise.all([elements.cam1Video.play(), elements.cam2Video.play()]);
        state.playing = true;
        state.animationFrame = requestAnimationFrame(videoPlaybackTick);
      } catch (_) { stopPlayback(); showToast("浏览器暂时无法播放该视频，请稍后重试"); }
    } else {
      stopPlayback();
    }
  } else {
    if (wasPlaying && resume) {
      // 从当前位置继续正常速度播放
      state.playStartedAt = performance.now(); state.playStartPosition = state.position;
      state.playing = true;
      state.animationFrame = requestAnimationFrame(rawPlaybackTick);
    } else {
      stopPlayback();
    }
  }
}
function rawPlaybackTick(now) {
  if (!state.playing || state.mode !== "raw") return;
  const factor = state.scrub ? state.scrub.direction * 2.5 : 1;
  const next = state.playStartPosition + factor * (now - state.playStartedAt) / 1000 / playbackDuration() * 10000;
  if (next >= 10000) { updateTimeline(10000, { immediate: true }); return stopPlayback(); }
  if (next <= 0) { updateTimeline(0, { immediate: true }); return stopPlayback(); }
  updateTimeline(next); state.animationFrame = requestAnimationFrame(rawPlaybackTick);
}
function videoPlaybackTick(now) {
  if (!state.playing || state.mode !== "video") return;
  const master = elements.cam1Video, follower = elements.cam2Video;
  if (state.scrub) {
    const dt = state.lastTickAt == null ? 16.7 : Math.min(now - state.lastTickAt, 100);
    state.lastTickAt = now;
    const target = master.currentTime + state.scrub.direction * 2.5 * dt / 1000;
    master.currentTime = Math.max(0, Math.min(target, playbackDuration() - 0.01));
    follower.currentTime = master.currentTime;
    updateTimelineDisplay(playbackDuration() ? master.currentTime / playbackDuration() * 10000 : 0);
  } else {
    if (Math.abs(follower.currentTime - master.currentTime) > 0.08) follower.currentTime = master.currentTime;
    updateTimelineDisplay(playbackDuration() ? master.currentTime / playbackDuration() * 10000 : 0);
    if (master.ended || master.currentTime >= playbackDuration() - 0.04) return stopPlayback();
  }
  state.animationFrame = requestAnimationFrame(videoPlaybackTick);
}
async function togglePlayback() {
  if (!state.metadata) return;
  if (state.scrub) { stopScrub(false); return; } // 快进快退中按空格 = 结束并保持暂停
  if (state.playing) return stopPlayback();
  if (state.position >= 10000) updateTimeline(0, { immediate: true });
  state.playing = true; elements.playIcon.textContent = "Ⅱ"; elements.playButton.setAttribute("aria-label", "暂停");
  if (state.mode === "video") {
    try {
      await Promise.all([elements.cam1Video.play(), elements.cam2Video.play()]);
      state.animationFrame = requestAnimationFrame(videoPlaybackTick);
    } catch (_) { stopPlayback(); showToast("浏览器暂时无法播放该视频，请稍后重试"); }
  } else {
    state.playStartedAt = performance.now(); state.playStartPosition = state.position;
    state.animationFrame = requestAnimationFrame(rawPlaybackTick);
  }
}

elements.subjectSearch.addEventListener("input", renderSubjects);
elements.refreshButton.addEventListener("click", () => loadSubjects());
elements.rawModeButton.addEventListener("click", activateRawMode);
elements.videoModeButton.addEventListener("click", activateVideoMode);
elements.generatePreviewButton.addEventListener("click", generatePreview);
elements.deletePreviewButton.addEventListener("click", deletePreview);
elements.exitAnnotationButton.addEventListener("click", exitAnnotation);
elements.applyDataRootButton.addEventListener("click", applyDataRoot);
elements.dataRootInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") applyDataRoot();
});
elements.timeline.addEventListener("input", (event) => { stopPlayback(); updateTimeline(Number(event.target.value)); });
elements.timeline.addEventListener("change", () => state.mode === "raw" && renderRawFrames());
elements.playButton.addEventListener("click", togglePlayback);
elements.stepBack.addEventListener("click", () => { stopPlayback(); updateTimeline(state.position - 10000 / Math.max(1, playbackDuration()), { immediate: true }); });
elements.stepForward.addEventListener("click", () => { stopPlayback(); updateTimeline(state.position + 10000 / Math.max(1, playbackDuration()), { immediate: true }); });
document.addEventListener("keydown", (event) => {
  // 空格绑定为播放/暂停(评分输入框聚焦时同样生效;数字输入不会用到空格字符)。
  // 数据目录是文本输入,路径可能含空格,聚焦它时空格留给路径本身。
  if (event.code === "Space" && event.target !== elements.dataRootInput) {
    event.preventDefault(); togglePlayback();
    return;
  }
  if (event.target.matches("input")) return;
  // 按住左右方向键 =  倍速快退/快进(event.repeat 为按住重复触发,忽略)
  if (event.code === "ArrowLeft" && !event.repeat) { event.preventDefault(); startScrub(-1); }
  if (event.code === "ArrowRight" && !event.repeat) { event.preventDefault(); startScrub(1); }
});
document.addEventListener("keyup", (event) => {
  if (event.code === "ArrowLeft" || event.code === "ArrowRight") stopScrub();
});
[elements.cam1Video, elements.cam2Video].forEach((video) => {
  video.addEventListener("loadedmetadata", () => {
    if (state.mode === "video") video.currentTime = playbackDuration() * state.position / 10000;
  });
  video.addEventListener("canplay", () => video.closest(".camera-card").classList.remove("loading"));
  video.addEventListener("waiting", () => {
    if (state.mode === "video") video.closest(".camera-card").classList.add("loading");
  });
  video.addEventListener("error", () => {
    if (state.mode !== "video" || state.videoErrorShown) return;
    state.videoErrorShown = true;
    const code = video.error?.code || "未知";
    showToast(`MP4 无法解码（媒体错误 ${code}），已切回精确逐帧模式`);
    activateRawMode();
  });
});
loadDataRoot();
loadSubjects({ autoSelect: true });
