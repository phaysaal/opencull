"use strict";

const state = {
  payload: null,
  clusters: [],
  decisions: new Map(),
  activeFilter: "all",
  query: "",
  visible: [],
  activeClusterId: null,
  compare: [],
  review: null,
  csrfToken: "",
  drafts: new Map(),
  saveQueue: Promise.resolve(),
  noteTimer: null,
  previewPoll: {},
  operationPlan: null,
  operationId: null,
  operationPoll: null,
};

const assessmentLabels = {
  pose_and_body: "Pose and body",
  eyes_and_gaze: "Eyes and gaze",
  mouth_and_expression: "Mouth and expression",
  readiness_and_timing: "Readiness and timing",
  interaction_and_moment: "Interaction and moment",
  occlusion_and_surroundings: "Occlusion and surroundings",
  irrecoverable_problems: "Irrecoverable problems",
  recoverable_raw_issues: "Recoverable RAW issues",
  distinctive_variations: "Distinctive variations",
  family_value: "Family value",
  uncertainty: "Uncertainty",
};

const $ = (selector) => document.querySelector(selector);

function imageUrl(name, size = "thumb") {
  return `/api/image?name=${encodeURIComponent(name)}&size=${encodeURIComponent(size)}`;
}

async function operationalPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-OpenCull-CSRF": state.csrfToken,
    },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function renderPreviewProgress(progress) {
  if (!progress) return;
  const active = progress.queued + progress.generating;
  $("#preview-status").textContent = active
    ? `${progress.generating} decoding · ${progress.queued} queued`
    : `${progress.ready} previews ready`;
  $("#cache-details").textContent =
    `${progress.cache.files} files · ${formatBytes(progress.cache.bytes)} · ` +
    `${progress.workers} bounded workers · ${progress.failed} failures`;
}

function previewElements(name, size) {
  return [...document.querySelectorAll("img[data-preview-name]")].filter(
    (image) => image.dataset.previewName === name &&
      image.dataset.previewSize === size);
}

function applyPreviewStates(result) {
  renderPreviewProgress(result.progress);
  for (const [name, preview] of Object.entries(result.previews || {})) {
    const size = preview.size || "thumb";
    for (const image of previewElements(name, size)) {
      const container = image.parentElement;
      const loading = container.querySelector(".image-loading, .viewer-loading");
      const error = container.querySelector(".image-error, .viewer-error");
      if (preview.status === "ready") {
        if (!image.hasAttribute("src")) image.src = imageUrl(name, size);
        if (loading) loading.hidden = true;
        if (error) error.hidden = true;
      } else if (preview.status === "failed") {
        if (loading) loading.hidden = true;
        if (error) {
          error.hidden = false;
          error.textContent = `Retry: ${preview.error || "preview failed"}`;
        }
      } else {
        if (loading) {
          loading.hidden = false;
          loading.textContent = preview.status === "generating"
            ? "Decoding preview…" : "Preview queued…";
        }
        if (error) error.hidden = true;
      }
    }
  }
}

async function pollPreviews(names, size) {
  clearTimeout(state.previewPoll[size]);
  if (!names.length) return;
  const query = names.map((name) => `name=${encodeURIComponent(name)}`).join("&");
  try {
    const response = await fetch(
      `/api/previews/status?size=${encodeURIComponent(size)}&${query}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "preview status failed");
    applyPreviewStates(result);
    const pending = Object.values(result.previews).some(
      (item) => ["queued", "generating", "deferred", "cancelled"].includes(item.status));
    if (pending) {
      state.previewPoll[size] = setTimeout(
        () => pollPreviews(names, size), 350);
    }
  } catch (error) {
    $("#preview-status").textContent = error.message;
  }
}

async function focusPreviews(visible, prefetch = [], size = "thumb") {
  try {
    const result = await operationalPost("/api/previews/focus", {
      visible, prefetch, size,
    });
    applyPreviewStates(result);
    pollPreviews(visible, size);
  } catch (error) {
    $("#preview-status").textContent = error.message;
  }
}

async function retryPreview(name, size) {
  try {
    const result = await operationalPost("/api/previews/retry", {name, size});
    applyPreviewStates({previews: {[name]: result}});
    pollPreviews([name], size);
  } catch (error) {
    $("#preview-status").textContent = error.message;
  }
}

function decisionFor(cluster) {
  return state.decisions.get(cluster.cluster_id);
}

function arraysEqual(left, right) {
  return left.length === right.length &&
    left.every((value) => right.includes(value));
}

function humanReview(clusterId) {
  return state.review.clusters[clusterId] || null;
}

function draftFor(cluster) {
  if (state.drafts.has(cluster.cluster_id)) {
    return state.drafts.get(cluster.cluster_id);
  }
  const existing = humanReview(cluster.cluster_id);
  const ai = decisionFor(cluster);
  const draft = {
    keepers: existing ? [...existing.keepers] : [...ai.photos],
    note: existing ? existing.note : "",
    reviewed: existing ? existing.reviewed : false,
  };
  state.drafts.set(cluster.cluster_id, draft);
  return draft;
}

function flags(cluster) {
  const decision = decisionFor(cluster);
  const human = humanReview(cluster.cluster_id);
  const reviewed = Boolean(human && human.reviewed);
  return {
    selected: decision.photos.length > 0,
    warning: Boolean(decision.warning),
    fallback: Boolean(decision.fallback),
    empty: decision.photos.length === 0,
    oversized: cluster.photos.length > 8,
    unreviewed: !reviewed,
    reviewed,
    modified: reviewed && !arraysEqual(human.keepers, decision.photos),
  };
}

function renderSummary() {
  const summary = state.payload.summary;
  const reviewStatus = state.review.status;
  const items = [
    ["Photos", summary.photos],
    ["Clusters", summary.clusters],
    ["AI keepers", summary.selected],
    ["Not selected", summary.not_selected],
    ["Warnings", summary.warnings],
    ["Fallbacks", summary.fallback_clusters],
    ["Missing", summary.missing_photos],
    ["Human reviewed", reviewStatus.reviewed_clusters],
    ["Human modified", reviewStatus.modified_clusters],
  ];
  const target = $("#summary");
  target.replaceChildren();
  for (const [label, value] of items) {
    const item = document.createElement("div");
    item.className = "stat";
    const strong = document.createElement("strong");
    strong.textContent = String(value);
    const span = document.createElement("span");
    span.textContent = label;
    item.append(strong, span);
    target.append(item);
  }
}

function clusterMatches(cluster) {
  const decision = decisionFor(cluster);
  const clusterFlags = flags(cluster);
  if (state.activeFilter !== "all" && !clusterFlags[state.activeFilter]) return false;
  if (!state.query) return true;
  const haystack = [
    cluster.cluster_id,
    ...cluster.photos,
    ...decision.photos,
    decision.rationale || "",
  ].join(" ").toLowerCase();
  return haystack.includes(state.query.toLowerCase());
}

function renderClusterList() {
  state.visible = state.clusters.filter(clusterMatches);
  $("#cluster-count").textContent = `${state.visible.length} of ${state.clusters.length} clusters`;
  const target = $("#cluster-list");
  target.replaceChildren();
  const fragment = document.createDocumentFragment();
  state.visible.forEach((cluster) => {
    const decision = decisionFor(cluster);
    const clusterFlags = flags(cluster);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cluster-item";
    button.dataset.id = cluster.cluster_id;
    if (cluster.cluster_id === state.activeClusterId) button.classList.add("active");

    const title = document.createElement("strong");
    title.textContent = cluster.cluster_id;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${cluster.photos.length} photos · ${decision.photos.length} kept`;
    const markers = document.createElement("span");
    markers.className = "markers";
    for (const key of ["warning", "fallback", "empty", "oversized", "reviewed", "modified"]) {
      if (clusterFlags[key]) {
        const marker = document.createElement("span");
        marker.className = `marker ${key}`;
        marker.title = key;
        markers.append(marker);
      }
    }
    const names = document.createElement("span");
    names.className = "filename-preview";
    names.textContent = cluster.photos.join(", ");
    button.append(title, markers, count, names);
    button.addEventListener("click", () => showCluster(cluster.cluster_id));
    fragment.append(button);
  });
  target.append(fragment);
}

function alert(message, kind = "") {
  const item = document.createElement("div");
  item.className = `alert ${kind}`.trim();
  item.textContent = message;
  return item;
}

function renderAlerts(cluster, decision) {
  const target = $("#cluster-alerts");
  target.replaceChildren();
  if (cluster.photos.length > 8) {
    target.append(alert(
      `${cluster.photos.length}-photo tournament cluster: compared through bounded rounds of at most eight images.`,
      "tournament",
    ));
  }
  if (decision.warning) target.append(alert(decision.warning));
  if (decision.fallback) {
    target.append(alert(
      "Fallback decision: the curator response was invalid, so scanner-ranked candidates were preserved for human review.",
      "fallback",
    ));
  }
  const missing = cluster.photos.filter((name) => state.payload.missing_photos.includes(name));
  if (missing.length) {
    target.append(alert(`Missing source files: ${missing.join(", ")}`, "missing"));
  }
}

function photoCard(cluster, decision, name, position) {
  const template = $("#photo-card-template");
  const card = template.content.firstElementChild.cloneNode(true);
  const aiSelected = decision.photos.includes(name);
  const draft = draftFor(cluster);
  const humanSelected = draft.keepers.includes(name);
  if (aiSelected) card.classList.add("selected");
  if (draft.reviewed && humanSelected) card.classList.add("human-selected");
  if (draft.reviewed && aiSelected && !humanSelected) card.classList.add("human-rejected");
  if (draft.reviewed && !aiSelected && humanSelected) card.classList.add("human-promoted");
  const image = card.querySelector("img");
  image.alt = name;
  image.dataset.previewName = name;
  image.dataset.previewSize = "thumb";
  image.addEventListener("error", () => {
    image.hidden = true;
    const error = card.querySelector(".image-error");
    error.hidden = false;
    error.textContent = "Retry preview";
  });
  card.querySelector(".filename").textContent = name;
  card.querySelector(".keeper-badge").textContent =
    aiSelected ? "AI keeper" : "AI did not select";
  const humanBadge = card.querySelector(".human-badge");
  humanBadge.textContent = draft.reviewed
    ? (humanSelected ? "Human keeper" : "Human rejects")
    : "Human unreviewed";
  const imageButton = card.querySelector(".image-button");
  imageButton.addEventListener("click", (event) => {
    if (!event.target.closest(".image-error")) openViewer([name]);
  });
  imageButton.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") openViewer([name]);
  });
  card.querySelector(".image-error").addEventListener("click", (event) => {
    event.stopPropagation();
    retryPreview(name, "thumb");
  });
  const metrics = state.payload.measurements[name];
  if (metrics) {
    const line = document.createElement("span");
    line.className = "photo-metrics";
    line.textContent = [
      metrics.technical_score != null ? `Technical ${metrics.technical_score}` : "",
      metrics.sharpness != null ? `Sharpness ${metrics.sharpness}` : "",
      metrics.captured || "",
    ].filter(Boolean).join(" · ");
    card.querySelector(".photo-info > div").append(line);
  }
  const toggle = card.querySelector(".human-toggle");
  toggle.textContent = humanSelected ? `Remove (${position + 1})` : `Keep (${position + 1})`;
  toggle.disabled = !state.review.status.compatible;
  toggle.addEventListener("click", () => toggleHumanKeeper(cluster, name));
  const compare = card.querySelector(".compare-button");
  if (state.compare.includes(name)) compare.classList.add("active");
  compare.addEventListener("click", () => toggleCompare(name));
  return card;
}

function renderAssessments(decision) {
  const target = $("#assessments");
  target.replaceChildren();
  const assessments = Array.isArray(decision.photographic_assessment)
    ? decision.photographic_assessment : [];
  if (!assessments.length) {
    const message = document.createElement("p");
    message.className = "muted";
    message.textContent = "No per-frame assessment was stored for this cluster.";
    target.append(message);
    return;
  }
  for (const assessment of assessments) {
    const article = document.createElement("article");
    article.className = "assessment";
    const title = document.createElement("h4");
    const confidence = Number(assessment.confidence);
    title.textContent = `${assessment.filename || "Unknown photo"}${
      Number.isFinite(confidence) && confidence > 0
        ? ` · ${Math.round(confidence * 100)}% observer confidence` : ""
    }`;
    const grid = document.createElement("dl");
    grid.className = "assessment-grid";
    for (const [field, label] of Object.entries(assessmentLabels)) {
      if (!(field in assessment)) continue;
      const wrapper = document.createElement("div");
      wrapper.className = "assessment-field";
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = String(assessment[field] || "Not assessed");
      wrapper.append(term, description);
      grid.append(wrapper);
    }
    article.append(title, grid);
    target.append(article);
  }
}

function showCluster(clusterId) {
  const cluster = state.clusters.find((item) => item.cluster_id === clusterId);
  if (!cluster) return;
  const decision = decisionFor(cluster);
  if (state.activeClusterId !== clusterId) state.compare = [];
  state.activeClusterId = clusterId;
  $("#empty-state").hidden = true;
  $("#cluster-view").hidden = false;
  const absolutePosition = state.clusters.indexOf(cluster) + 1;
  $("#cluster-position").textContent = `CLUSTER ${absolutePosition} OF ${state.clusters.length}`;
  $("#cluster-title").textContent = cluster.cluster_id;
  $("#cluster-subtitle").textContent =
    `${cluster.photos.length} photographs · ${decision.photos.length} recommended keeper${decision.photos.length === 1 ? "" : "s"}`;
  $("#confidence").textContent = `${Math.round(Number(decision.confidence || 0) * 100)}% curator confidence`;
  $("#rationale").textContent = decision.rationale || "No rationale stored.";
  $("#raw-decision").textContent = JSON.stringify(decision, null, 2);
  renderAlerts(cluster, decision);
  const grid = $("#photo-grid");
  grid.replaceChildren(...cluster.photos.map(
    (name, position) => photoCard(cluster, decision, name, position)));
  renderHumanReview(cluster, decision);
  renderAssessments(decision);
  renderClusterList();
  updateNavigation();
  $(".review").scrollTo({top: 0, behavior: "auto"});
  persistPosition(clusterId);
  const adjacent = [];
  for (const offset of [-1, 1]) {
    const neighbor = state.clusters[absolutePosition - 1 + offset];
    if (neighbor) adjacent.push(...neighbor.photos);
  }
  focusPreviews(cluster.photos, adjacent, "thumb");
}

function updateNavigation() {
  const index = state.visible.findIndex((item) => item.cluster_id === state.activeClusterId);
  $("#previous-cluster").disabled = index <= 0;
  $("#next-cluster").disabled = index < 0 || index >= state.visible.length - 1;
}

function navigate(direction) {
  const index = state.visible.findIndex((item) => item.cluster_id === state.activeClusterId);
  const next = state.visible[index + direction];
  if (next) showCluster(next.cluster_id);
}

function toggleCompare(name) {
  const index = state.compare.indexOf(name);
  if (index >= 0) {
    state.compare.splice(index, 1);
  } else {
    if (state.compare.length === 2) state.compare.shift();
    state.compare.push(name);
  }
  const active = state.activeClusterId;
  showCluster(active);
  if (state.compare.length === 2) openViewer(state.compare);
}

function setSaveStatus(message, kind = "") {
  const target = $("#save-status");
  target.textContent = message;
  target.className = `save-status ${kind}`.trim();
}

function applyReviewState(review) {
  state.review = review;
  renderSummary();
  renderClusterList();
  const active = state.clusters.find(
    (item) => item.cluster_id === state.activeClusterId);
  if (active) {
    const stored = humanReview(active.cluster_id);
    if (stored) {
      state.drafts.set(active.cluster_id, {
        keepers: [...stored.keepers],
        note: stored.note,
        reviewed: stored.reviewed,
      });
    }
    renderHumanReview(active, decisionFor(active));
    const grid = $("#photo-grid");
    grid.replaceChildren(...active.photos.map(
      (name, position) => photoCard(active, decisionFor(active), name, position)));
  }
}

function enqueueSave(path, buildBody, successMessage = "Human review saved") {
  setSaveStatus("Saving…", "saving");
  state.saveQueue = state.saveQueue.then(async () => {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-OpenCull-CSRF": state.csrfToken,
      },
      body: JSON.stringify({
        ...buildBody(),
        revision: state.review.revision,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Save failed (${response.status})`);
    applyReviewState(payload);
    setSaveStatus(successMessage);
  }).catch((error) => {
    setSaveStatus(error.message, "error");
  });
  return state.saveQueue;
}

function saveCluster(cluster, draft, message = "Human review saved") {
  state.drafts.set(cluster.cluster_id, {
    keepers: [...draft.keepers],
    note: draft.note,
    reviewed: draft.reviewed,
  });
  return enqueueSave("/api/review/cluster", () => {
    const current = state.drafts.get(cluster.cluster_id);
    return {
      cluster_id: cluster.cluster_id,
      keepers: current.keepers,
      note: current.note,
      reviewed: current.reviewed,
    };
  }, message);
}

function persistPosition(clusterId) {
  if (!state.review || !state.review.status.compatible ||
      state.review.last_cluster_id === clusterId) return;
  enqueueSave("/api/review/position", () => ({cluster_id: clusterId}), "Position saved");
}

function toggleHumanKeeper(cluster, name) {
  const draft = draftFor(cluster);
  const keepers = [...draft.keepers];
  const index = keepers.indexOf(name);
  if (index >= 0) keepers.splice(index, 1);
  else keepers.push(name);
  const updated = {...draft, keepers, reviewed: true};
  saveCluster(cluster, updated);
  showCluster(cluster.cluster_id);
}

function renderHumanReview(cluster, decision) {
  const draft = draftFor(cluster);
  const modified = draft.reviewed && !arraysEqual(draft.keepers, decision.photos);
  const label = $("#review-state");
  label.className = "review-state";
  if (!state.review.status.compatible) {
    label.textContent = "Review file incompatible";
    label.classList.add("modified");
  } else if (!draft.reviewed) {
    label.textContent = "Unreviewed";
  } else if (modified) {
    label.textContent = "Human modified";
    label.classList.add("modified");
  } else {
    label.textContent = "Human agrees with AI";
    label.classList.add("reviewed");
  }
  const note = $("#review-note");
  if (note.value !== draft.note) note.value = draft.note;
  const disabled = !state.review.status.compatible;
  note.disabled = disabled;
  ["#accept-ai", "#keep-none", "#mark-reviewed", "#reset-review"].forEach(
    (selector) => { $(selector).disabled = disabled; });
}

function updateActiveReview(transform, message) {
  const cluster = state.clusters.find(
    (item) => item.cluster_id === state.activeClusterId);
  if (!cluster) return;
  const updated = transform({...draftFor(cluster), keepers: [...draftFor(cluster).keepers]});
  saveCluster(cluster, updated, message);
  showCluster(cluster.cluster_id);
}

async function exportReviewedResult() {
  try {
    const response = await fetch("/api/export");
    if (!response.ok) throw new Error(`Export failed (${response.status})`);
    const payload = await response.json();
    const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"], {
      type: "application/json",
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "opencull-reviewed-result.json";
    link.click();
    URL.revokeObjectURL(link.href);
    setSaveStatus("Reviewed result exported");
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function downloadSelection(format) {
  const policy = $("#export-policy").value;
  try {
    const response = await fetch(
      `/api/export/file?policy=${encodeURIComponent(policy)}&format=${encodeURIComponent(format)}`);
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `Export failed (${response.status})`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = match ? match[1] : `opencull-selection.${format}`;
    link.click();
    URL.revokeObjectURL(link.href);
    setSaveStatus(`${format.toUpperCase()} selection exported`);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function planStat(label, value) {
  const item = document.createElement("div");
  item.className = "plan-stat";
  const strong = document.createElement("strong");
  strong.textContent = String(value);
  const span = document.createElement("span");
  span.textContent = label;
  item.append(strong, span);
  return item;
}

function renderPlan(plan) {
  state.operationPlan = plan;
  $("#preflight-result").hidden = false;
  $("#plan-title").textContent =
    `${plan.action.toUpperCase()} · ${plan.layout} · ${plan.plan_id}`;
  const errors = plan.summary.errors;
  $("#plan-status").textContent = errors.length
    ? "Preflight blocked" : "Preflight passed";
  $("#plan-status").className = errors.length ? "save-status error" : "save-status";
  $("#plan-summary").replaceChildren(
    planStat("Files", plan.summary.files),
    planStat("Selected", plan.summary.selected_files),
    planStat("Companions", plan.summary.companion_files),
    planStat("Required", formatBytes(plan.summary.bytes)),
    planStat("Free", formatBytes(plan.summary.free_bytes)),
  );
  const alerts = $("#plan-errors");
  alerts.replaceChildren();
  errors.forEach((message) => alerts.append(alert(message, "fallback")));
  const rows = $("#operation-rows");
  rows.replaceChildren();
  for (const item of plan.items) {
    const row = document.createElement("tr");
    for (const value of [
      item.cluster_id, item.source, item.destination, formatBytes(item.bytes),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.append(cell);
    }
    rows.append(row);
  }
  const expected = `${plan.action === "move" ? "MOVE" : "COPY"} ${plan.plan_id}`;
  $("#confirmation-help").textContent =
    `Type exactly “${expected}” after reviewing every operation.`;
  $("#operation-confirmation").value = "";
  $("#execute-operation").disabled = true;
  $("#execute-operation").dataset.expected = expected;
}

async function runPreflight() {
  try {
    const plan = await operationalPost("/api/action/preflight", {
      destination: $("#operation-destination").value,
      policy: $("#export-policy").value,
      action: $("#operation-action").value,
      layout: $("#operation-layout").value,
      preserve_relative: $("#preserve-relative").checked,
      include_companions: $("#include-companions").checked,
    });
    renderPlan(plan);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function renderOperation(operation) {
  $("#operation-progress").hidden = false;
  $("#operation-journal").textContent = JSON.stringify(operation, null, 2);
  const total = operation.items
    ? operation.items.length : operation.total_files || 0;
  const completed = operation.completed_files || 0;
  $("#operation-progress-bar").max = Math.max(1, total);
  $("#operation-progress-bar").value = completed;
  $("#operation-progress-text").textContent =
    `${operation.status} · ${completed} of ${total} files · ` +
    `${formatBytes(operation.completed_bytes || 0)} verified`;
  const running = ["planned", "running", "paused"].includes(operation.status);
  $("#cancel-operation").hidden = !running;
  $("#rollback-operation").hidden =
    operation.action !== "move" ||
    !["completed", "failed", "paused"].includes(operation.status);
}

async function monitorOperation(operationId) {
  clearTimeout(state.operationPoll);
  try {
    const response = await fetch(
      `/api/action/status?id=${encodeURIComponent(operationId)}`);
    const operation = await response.json();
    if (!response.ok) throw new Error(operation.error || "status failed");
    renderOperation(operation);
    if (["running", "planned"].includes(operation.status)) {
      state.operationPoll = setTimeout(
        () => monitorOperation(operationId), 500);
    }
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function executePlan() {
  const plan = state.operationPlan;
  if (!plan) return;
  try {
    const operation = await operationalPost("/api/action/execute", {
      plan_id: plan.plan_id,
      confirmation: $("#operation-confirmation").value,
    });
    state.operationId = operation.plan_id;
    renderOperation(operation);
    monitorOperation(operation.plan_id);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function createContactSheets() {
  const destination = $("#operation-destination").value;
  if (!destination) {
    setSaveStatus("Choose a destination first", "error");
    return;
  }
  const confirmation = window.prompt(
    "Type exactly: CREATE CONTACT SHEETS");
  if (confirmation !== "CREATE CONTACT SHEETS") return;
  try {
    const operation = await operationalPost("/api/action/contact-sheet", {
      destination,
      policy: $("#export-policy").value,
      confirmation,
      columns: 4,
      rows: 5,
    });
    state.operationId = operation.plan_id;
    renderOperation(operation);
    monitorOperation(operation.plan_id);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function cancelOperation() {
  if (!state.operationId) return;
  try {
    const operation = await operationalPost("/api/action/cancel", {
      operation_id: state.operationId,
    });
    renderOperation(operation);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function rollbackOperation() {
  if (!state.operationId) return;
  const expected = `ROLLBACK ${state.operationId}`;
  const confirmation = window.prompt(`Type exactly: ${expected}`);
  if (confirmation !== expected) return;
  try {
    const operation = await operationalPost("/api/action/rollback", {
      operation_id: state.operationId,
      confirmation,
    });
    renderOperation(operation);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function resumeOperation() {
  const journalPath = $("#resume-journal-path").value.trim();
  if (!journalPath) return;
  const planId = window.prompt(
    "Enter the plan ID shown inside the journal");
  if (!planId) return;
  const confirmation = `RESUME ${planId}`;
  if (!window.confirm(`Resume with confirmation “${confirmation}”?`)) return;
  try {
    const operation = await operationalPost("/api/action/resume", {
      journal_path: journalPath,
      confirmation,
    });
    state.operationId = operation.plan_id;
    renderOperation(operation);
    monitorOperation(operation.plan_id);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function openViewer(names) {
  const viewer = $("#viewer");
  $("#viewer-title").textContent = names.join(" ↔ ");
  const target = $("#viewer-images");
  target.style.setProperty("--viewer-columns", String(names.length));
  target.replaceChildren();
  for (const name of names) {
    const pane = document.createElement("div");
    pane.className = "viewer-pane";
    const image = document.createElement("img");
    image.alt = name;
    image.dataset.previewName = name;
    image.dataset.previewSize = "detail";
    image.addEventListener("click", () => image.classList.toggle("actual"));
    const loading = document.createElement("span");
    loading.className = "viewer-loading";
    loading.textContent = "Detailed preview queued…";
    const error = document.createElement("button");
    error.type = "button";
    error.className = "viewer-error";
    error.hidden = true;
    error.addEventListener("click", () => retryPreview(name, "detail"));
    pane.append(image, loading, error);
    target.append(pane);
  }
  viewer.showModal();
  focusPreviews(names, [], "detail");
}

function renderDetails() {
  const payload = state.payload;
  const report = payload.report;
  const fields = [
    ["Report", payload.report_path],
    ["Photos", payload.photos_root],
    ["SHA-256", payload.report_sha256],
    ["Format", report.format],
    ["Manifest SHA", report.manifest_sha256],
    ["Notice", report.notice],
    ["Scanner manifest", payload.manifest_path || "Not supplied"],
    ["Mode", "Read-only"],
  ];
  const list = $("#report-details");
  list.replaceChildren();
  for (const [label, value] of fields) {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = String(value || "—");
    list.append(term, description);
  }
  $("#adaptive-json").textContent = JSON.stringify(report.adaptive_clustering, null, 2);
  $("#metadata-json").textContent = JSON.stringify({
    format: report.format,
    manifest_sha256: report.manifest_sha256,
    notice: report.notice,
    warnings: report.warnings,
  }, null, 2);
}

function bindEvents() {
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    renderClusterList();
    updateNavigation();
  });
  $("#filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    state.activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderClusterList();
    updateNavigation();
  });
  $("#previous-cluster").addEventListener("click", () => navigate(-1));
  $("#next-cluster").addEventListener("click", () => navigate(1));
  $("#accept-ai").addEventListener("click", () => updateActiveReview((draft) => {
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    return {...draft, keepers: [...decisionFor(cluster).photos], reviewed: true};
  }, "AI recommendation accepted"));
  $("#keep-none").addEventListener("click", () => updateActiveReview(
    (draft) => ({...draft, keepers: [], reviewed: true}), "No keepers selected"));
  $("#mark-reviewed").addEventListener("click", () => updateActiveReview(
    (draft) => ({...draft, reviewed: true}), "Cluster marked reviewed"));
  $("#reset-review").addEventListener("click", () => updateActiveReview((draft) => {
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    return {...draft, keepers: [...decisionFor(cluster).photos], reviewed: false};
  }, "Cluster returned to unreviewed"));
  $("#review-note").addEventListener("input", (event) => {
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    if (!cluster) return;
    const draft = draftFor(cluster);
    draft.note = event.target.value;
    clearTimeout(state.noteTimer);
    state.noteTimer = setTimeout(
      () => saveCluster(cluster, draft, "Reviewer note saved"), 650);
  });
  $("#export-button").addEventListener("click", exportReviewedResult);
  $("#organize-button").addEventListener(
    "click", () => $("#organize").showModal());
  $("#close-organize").addEventListener(
    "click", () => $("#organize").close());
  document.querySelectorAll(".download-format").forEach((button) => {
    button.addEventListener(
      "click", () => downloadSelection(button.dataset.format));
  });
  $("#run-preflight").addEventListener("click", runPreflight);
  $("#operation-confirmation").addEventListener("input", (event) => {
    const expected = $("#execute-operation").dataset.expected || "";
    $("#execute-operation").disabled =
      event.target.value !== expected ||
      Boolean(state.operationPlan?.summary.errors.length);
  });
  $("#execute-operation").addEventListener("click", executePlan);
  $("#cancel-operation").addEventListener("click", cancelOperation);
  $("#rollback-operation").addEventListener("click", rollbackOperation);
  $("#create-contact-sheets").addEventListener("click", createContactSheets);
  $("#resume-operation").addEventListener("click", resumeOperation);
  $("#clear-cache").addEventListener("click", async () => {
    if (!window.confirm(
      "Delete generated previews? Originals and reports will not be touched.")) return;
    try {
      const result = await operationalPost("/api/cache/clear", {});
      renderPreviewProgress(result.progress);
      document.querySelectorAll("img[data-preview-name]").forEach((image) => {
        image.removeAttribute("src");
      });
      const cluster = state.clusters.find(
        (item) => item.cluster_id === state.activeClusterId);
      if (cluster) focusPreviews(cluster.photos, [], "thumb");
    } catch (error) {
      $("#preview-status").textContent = error.message;
    }
  });
  $("#close-viewer").addEventListener("click", () => $("#viewer").close());
  $("#viewer").addEventListener("click", (event) => {
    if (event.target === $("#viewer")) $("#viewer").close();
  });
  $("#about-button").addEventListener("click", () => $("#about").showModal());
  $("#close-about").addEventListener("click", () => $("#about").close());
  document.addEventListener("keydown", (event) => {
    if ($("#viewer").open || $("#about").open || event.target.matches("input")) return;
    if (event.key === "ArrowLeft") navigate(-1);
    if (event.key === "ArrowRight") navigate(1);
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    if (!cluster || !state.review.status.compatible) return;
    const number = Number(event.key);
    if (Number.isInteger(number) && number >= 1 && number <= 9 &&
        cluster.photos[number - 1]) {
      toggleHumanKeeper(cluster, cluster.photos[number - 1]);
    }
    if (event.key.toLowerCase() === "a") $("#accept-ai").click();
    if (event.key.toLowerCase() === "n") $("#keep-none").click();
    if (event.key.toLowerCase() === "u") $("#reset-review").click();
  });
}

async function initialize() {
  bindEvents();
  try {
    const response = await fetch("/api/report");
    if (!response.ok) throw new Error(`Report request failed (${response.status})`);
    state.payload = await response.json();
    state.review = state.payload.review;
    state.csrfToken = state.payload.csrf_token;
    state.clusters = state.payload.report.clusters;
    state.decisions = new Map(
      state.payload.report.keep.map((item) => [item.cluster_id, item]),
    );
    renderSummary();
    renderClusterList();
    renderDetails();
    $("#report-status").textContent =
      `${state.payload.summary.photos} photos · ${state.payload.summary.clusters} clusters`;
    if (!state.review.status.compatible) {
      setSaveStatus(state.review.status.stale_reason, "error");
    }
    const resumeId = state.review.last_cluster_id;
    const initial = state.clusters.some((item) => item.cluster_id === resumeId)
      ? resumeId : state.clusters[0]?.cluster_id;
    if (initial) showCluster(initial);
  } catch (error) {
    $("#report-status").textContent = "Could not load report";
    $("#empty-state").innerHTML = "";
    const heading = document.createElement("h2");
    heading.textContent = "Report loading failed";
    const message = document.createElement("p");
    message.textContent = error.message;
    $("#empty-state").append(heading, message);
  }
}

initialize();
