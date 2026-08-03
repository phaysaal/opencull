"use strict";

const state = {
  payload: null,
  project: null,
  clusters: [],
  decisions: new Map(),
  activeFilter: "all",
  query: "",
  visible: [],
  activeClusterId: null,
  activeReviewPhoto: null,
  inspectorOpen: true,
  inspectorTab: "group",
  clusterWindowStart: 0,
  clusterWindowSize: 200,
  compare: [],
  review: null,
  csrfToken: "",
  drafts: new Map(),
  saveQueue: Promise.resolve(),
  noteTimer: null,
  previewPoll: {},
  previewGeneration: {},
  operationPlan: null,
  operationId: null,
  operationPoll: null,
  recoverableOperations: [],
  recoveryIssues: new Map(),
  projectMigration: null,
  viewer: {zoom: 1, x: 0, y: 0, crop: "fit"},
  viewDensity: "compact",
  jobs: null,
  jobPoll: null,
  providers: null,
  people: null,
  activePersonId: null,
  personFacePages: new Map(),
  personPoll: null,
  peopleQuery: "",
  peopleFilter: "all",
  mergeSelection: new Set(),
  faceSelection: new Set(),
  toastTimer: null,
  workspaceMode: "review",
  shortlist: null,
  shortlistReview: null,
  shortlistVisible: [],
  activeShortlistPhoto: null,
  shortlistQuery: "",
  shortlistDefaultPath: "",
  shortlistJobPoll: null,
  shortlistLoadedJobId: null,
  rawSource: null,
  editDirections: null,
  editDirectionJobId: null,
  stylePhotoPaths: [],
  styleHiddenPaths: new Set(),
  styleCandidatePaths: new Set(),
  styleExtractionJobId: null,
  development: null,
  activeDevelopPhoto: null,
  activeDevelopVariant: "calibrated",
  activeDevelopEngine: null,
  developmentCompareMode: "treatment",
  developmentJobId: null,
  comparisonJobId: null,
  rendererExportJobId: null,
  deliveryExportJobId: null,
  lastDevelopmentExportPath: null,
  developmentPreviewUrls: new Map(),
  developmentThumbnailUrls: new Map(),
  developmentThumbnailTasks: new Map(),
  developmentCacheWarmup: null,
  developmentLastInteraction: 0,
};

const productStatusLanguage = {
  ready: "Ready",
  working: "In progress",
  paused: "Paused safely",
  attention: "Needs attention",
  complete: "Completed",
  verified: "Completed and verified",
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

const professionalAssessmentLabels = {
  composition: "Composition",
  angle_and_perspective: "Angle and perspective",
  subject_presentation: "Subject presentation",
  pose_and_expression: "Pose and expression",
  moment_and_emotion: "Moment and emotion",
  light_and_tonality: "Light and tonality",
  surroundings: "Surroundings",
  irrecoverable_defects: "Irrecoverable defects",
  raw_editing_opportunities: "RAW editing opportunities",
  distinctiveness: "Distinctiveness",
};

const $ = (selector) => document.querySelector(selector);

const previewImageLoads = new WeakMap();
let previewVisibilityTimer = null;
let previewObserver = null;

function previewFileUrl(name, size, revision) {
  return `/api/previews/file?name=${encodeURIComponent(name)}` +
    `&size=${encodeURIComponent(size)}&revision=${encodeURIComponent(revision)}`;
}

function previewChrome(image) {
  const container = image.parentElement;
  return {
    loading: container?.querySelector(".image-loading, .viewer-loading") || null,
    error: container?.querySelector(".image-error, .viewer-error") || null,
  };
}

function previewMatches(image, name, size) {
  return image.isConnected && image.dataset.previewName === name &&
    image.dataset.previewSize === size;
}

function scheduleVisiblePreviewFocus() {
  clearTimeout(previewVisibilityTimer);
  previewVisibilityTimer = setTimeout(() => {
    for (const size of ["thumb", "detail"]) {
      const names = [...new Set(
        [...document.querySelectorAll(
          `img[data-preview-visible="true"][data-preview-size="${size}"]`)]
          .filter((image) => image.isConnected)
          .map((image) => image.dataset.previewName)
          .filter(Boolean),
      )];
      if (names.length) void focusPreviews(names, [], size);
    }
  }, 45);
}

function observePreviewImage(image) {
  if (!("IntersectionObserver" in window)) {
    image.dataset.previewVisible = "true";
    scheduleVisiblePreviewFocus();
    return;
  }
  if (!previewObserver) {
    previewObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        entry.target.dataset.previewVisible = String(entry.isIntersecting);
      }
      scheduleVisiblePreviewFocus();
    }, {rootMargin: "240px"});
  }
  previewObserver.observe(image);
}

function bindPreviewImage(image, name, size = "thumb") {
  const changed = image.dataset.previewName !== name ||
    image.dataset.previewSize !== size;
  image.dataset.previewName = name;
  image.dataset.previewSize = size;
  if (changed) {
    previewImageLoads.set(image, Symbol("preview-binding"));
    delete image.dataset.previewRevision;
    image.classList.add("preview-loading");
    const {loading, error} = previewChrome(image);
    if (loading) {
      loading.hidden = false;
      loading.textContent = "Preparing preview…";
    }
    if (error) error.hidden = true;
  }
  observePreviewImage(image);
}

function unbindPreviewImage(image) {
  previewImageLoads.set(image, Symbol("preview-unbound"));
  previewObserver?.unobserve(image);
  delete image.dataset.previewName;
  delete image.dataset.previewSize;
  delete image.dataset.previewVisible;
  delete image.dataset.previewRevision;
  image.classList.remove("preview-loading");
}

async function commitDecodedPreview(image, name, size, revision) {
  if (!revision || !previewMatches(image, name, size)) return;
  const url = previewFileUrl(name, size, revision);
  if (image.dataset.previewRevision === revision && image.complete &&
      image.naturalWidth > 0) {
    image.hidden = false;
    image.classList.remove("preview-loading");
    const {loading, error} = previewChrome(image);
    if (loading) loading.hidden = true;
    if (error) error.hidden = true;
    return;
  }
  const token = Symbol("preview-decode");
  previewImageLoads.set(image, token);
  const candidate = new Image();
  candidate.decoding = "async";
  candidate.src = url;
  try {
    if (typeof candidate.decode === "function") {
      await candidate.decode();
    } else {
      await new Promise((resolve, reject) => {
        candidate.addEventListener("load", resolve, {once: true});
        candidate.addEventListener("error", reject, {once: true});
      });
    }
  } catch (_) {
    if (previewImageLoads.get(image) !== token ||
        !previewMatches(image, name, size)) return;
    image.classList.remove("preview-loading");
    const {loading, error} = previewChrome(image);
    if (loading) loading.hidden = true;
    if (error) {
      error.hidden = false;
      error.textContent = "Preview could not be displayed · Retry";
    }
    return;
  }
  if (previewImageLoads.get(image) !== token ||
      !previewMatches(image, name, size)) return;
  image.addEventListener("load", () => {
    if (!previewMatches(image, name, size)) return;
    image.dataset.previewRevision = revision;
    image.hidden = false;
    image.classList.remove("preview-loading");
    const {loading, error} = previewChrome(image);
    if (loading) loading.hidden = true;
    if (error) error.hidden = true;
  }, {once: true});
  image.addEventListener("error", () => {
    if (!previewMatches(image, name, size)) return;
    image.classList.remove("preview-loading");
    const {loading, error} = previewChrome(image);
    if (loading) loading.hidden = true;
    if (error) {
      error.hidden = false;
      error.textContent = "Preview could not be displayed · Retry";
    }
  }, {once: true});
  image.src = url;
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
  $("#preview-activity").textContent = active
    ? `${active} active` : progress.failed ? `${progress.failed} failed` : "Idle";
  $("#preview-activity").className =
    progress.failed ? "attention" : active ? "active" : "";
  const details =
    `${progress.cache.files} files · ${formatBytes(progress.cache.bytes)} · ` +
    `${progress.workers} bounded workers · ${progress.failed} failures`;
  $("#cache-details").textContent = details;
  $("#queue-preview-status").textContent = active
    ? `${progress.generating} decoding · ${progress.queued} queued`
    : progress.failed ? `${progress.failed} preview failure${progress.failed === 1 ? "" : "s"}`
      : "Preview processing idle";
  $("#queue-preview-details").textContent = details;
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
        void commitDecodedPreview(image, name, size, preview.revision);
      } else if (preview.status === "failed") {
        image.classList.remove("preview-loading");
        if (loading) loading.hidden = true;
        if (error) {
          error.hidden = false;
          error.textContent = "Preview unavailable · Retry";
          error.title = preview.error || "Preview generation failed";
        }
      } else {
        image.classList.add("preview-loading");
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

async function pollPreviews(names, size, generation) {
  if (!names.length || state.previewGeneration[size] !== generation) return;
  clearTimeout(state.previewPoll[size]);
  const query = names.map((name) => `name=${encodeURIComponent(name)}`).join("&");
  try {
    const response = await fetch(
      `/api/previews/status?size=${encodeURIComponent(size)}&${query}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "preview status failed");
    if (state.previewGeneration[size] !== generation) return;
    applyPreviewStates(result);
    const pending = Object.values(result.previews).some(
      (item) => ["queued", "generating", "deferred", "cancelled"].includes(item.status));
    if (pending) {
      state.previewPoll[size] = setTimeout(
        () => pollPreviews(names, size, generation), 350);
    }
  } catch (error) {
    if (state.previewGeneration[size] !== generation) return;
    $("#preview-status").textContent = error.message;
    $("#queue-preview-status").textContent = `Preview status unavailable: ${error.message}`;
    state.previewPoll[size] = setTimeout(
      () => pollPreviews(names, size, generation), 1000);
  }
}

async function focusPreviews(visible, prefetch = [], size = "thumb") {
  const generation = Number(state.previewGeneration[size] || 0) + 1;
  state.previewGeneration[size] = generation;
  clearTimeout(state.previewPoll[size]);
  try {
    const result = await operationalPost("/api/previews/focus", {
      visible, prefetch, size,
    });
    if (state.previewGeneration[size] !== generation) return;
    applyPreviewStates(result);
    pollPreviews(visible, size, generation);
  } catch (error) {
    if (state.previewGeneration[size] !== generation) return;
    $("#preview-status").textContent = error.message;
    $("#queue-preview-status").textContent = `Preview request failed: ${error.message}`;
    state.previewPoll[size] = setTimeout(
      () => focusPreviews(visible, prefetch, size), 1000);
  }
}

async function retryPreview(name, size) {
  try {
    const result = await operationalPost("/api/previews/retry", {name, size});
    applyPreviewStates({previews: {[name]: result}});
    void focusPreviews([name], [], size);
  } catch (error) {
    $("#preview-status").textContent = error.message;
    $("#queue-preview-status").textContent = `Preview retry failed: ${error.message}`;
  }
}

function jobActionButton(job, label, action, kind = "quiet-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = kind;
  button.textContent = label;
  button.addEventListener("click", async () => {
    try {
      state.jobs = await operationalPost("/api/jobs/action", {
        job_id: job.id, action,
      });
      renderJobs();
    } catch (error) {
      setJobFormStatus(error.message, "error");
    }
  });
  return button;
}

const jobStatusPresentation = {
  queued: {label: "Waiting", tone: "neutral", stage: "Queued for sequential processing"},
  running: {label: "Culling", tone: "active", stage: "AI review in progress"},
  stopping: {label: "Pausing", tone: "warning", stage: "Saving a safe stopping point"},
  detached: {label: "Monitoring", tone: "warning", stage: "Process survived an app restart"},
  paused: {label: "Paused", tone: "warning", stage: "Validated checkpoint available"},
  failed: {label: "Needs attention", tone: "danger", stage: "Stopped before completion"},
  cancelled: {label: "Cancelled", tone: "neutral", stage: "Checkpoint retained when available"},
  completed: {label: "Ready to review", tone: "success", stage: "Culling report completed"},
};

function jobTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(date);
}

function jobKindLabel(job) {
  return {
    culling: "Culling",
    professional_shortlist: "Professional Shortlist",
    edit_suggestions: "Edit Directions",
    semantic_verification: "Semantic Verification",
    style_profile: "Personal Style",
    development_render: "Photo Development",
    development_pipeline: "Guided Development",
    renderer_comparison: "Renderer Comparison",
    renderer_export: "Full-size darktable render",
    delivery_export: "Image Export",
  }[job.kind || "culling"] || "Darkimiya Job";
}

function jobFolderLabel(job) {
  const firstExample = job.photo_examples?.[0];
  const source = job.kind === "style_profile" && firstExample
    ? firstExample.replace(/[\\/][^\\/]+$/, "")
    : job.photos || job.original || job.developed || job.output || "";
  return source.split(/[\\/]/).filter(Boolean).pop() || "Unknown folder";
}

function jobDisplayName(job) {
  return `${jobKindLabel(job)} — ${jobFolderLabel(job)}`;
}

function renderQueueOverview(jobs) {
  const count = (statuses) => jobs.filter((job) => statuses.includes(job.status)).length;
  $("#queue-running-count").textContent =
    String(count(["running", "stopping", "detached"]));
  $("#queue-waiting-count").textContent = String(count(["queued"]));
  $("#queue-completed-count").textContent = String(count(["completed"]));
  $("#queue-attention-count").textContent =
    String(count(["failed", "paused"]));
  const active = count(["running", "stopping", "detached"]);
  const waiting = count(["queued"]);
  const attention = count(["failed", "paused"]);
  $("#culling-activity").textContent = active ? `${active} running`
    : waiting ? `${waiting} queued` : attention ? `${attention} paused` : "Idle";
  $("#culling-activity").className =
    attention && !active ? "attention" : active ? "active" : "";
}

function renderJobs() {
  if (!state.jobs) return;
  const target = $("#job-list");
  target.replaceChildren();
  const visibleJobs = state.jobs.jobs.filter((job) =>
    !job.kind || job.kind === "culling");
  renderQueueOverview(visibleJobs);
  const active = visibleJobs.find((job) =>
    ["running", "stopping", "detached"].includes(job.status));
  const waiting = visibleJobs.filter((job) => job.status === "queued").length;
  $("#queue-status").textContent = active
    ? `One supervised process is active${waiting ? ` · ${waiting} waiting` : ""}`
    : waiting ? `${waiting} waiting · worker will start automatically`
      : "Worker idle · ready for another folder";
  visibleJobs.forEach((job, jobIndex) => {
    const basePresentation = jobStatusPresentation[job.status] || {
      label: job.status, tone: "neutral", stage: job.message,
    };
    const progressModel = job.kind === "style_profile"
      ? styleProfileProgress(job) : job.progress;
    const presentation = job.kind === "style_profile"
      ? {
          ...basePresentation,
          label: job.status === "completed" ? "Profile ready" : "Style profile",
          stage: progressModel.stage,
        }
      : basePresentation;
    const article = document.createElement("article");
    article.className = `job-card job-${job.status}`;
    article.dataset.jobId = job.id;
    const header = document.createElement("div");
    header.className = "job-card-header";
    const identity = document.createElement("div");
    const statusMark = document.createElement("span");
    statusMark.className = `job-status-mark ${presentation.tone}`;
    statusMark.setAttribute("aria-hidden", "true");
    const identityText = document.createElement("div");
    const title = document.createElement("strong");
    const jobSource = job.photos || job.original || job.developed || job.output;
    const stylePhotoCount = job.photo_examples?.length || 0;
    const sourceLabel = job.kind === "style_profile" && stylePhotoCount
      ? `${stylePhotoCount} selected finished photograph${stylePhotoCount === 1 ? "" : "s"}`
      : jobSource;
    title.textContent = jobDisplayName(job);
    const path = document.createElement("span");
    path.className = "muted";
    path.textContent = job.kind === "style_profile"
      ? `${sourceLabel} · Added ${jobTime(job.created_at)}`
      : `${jobSource} · Added ${jobTime(job.created_at)}`;
    identityText.append(title, path);
    identity.append(statusMark, identityText);
    const badge = document.createElement("span");
    badge.className = `job-badge ${job.status} ${presentation.tone}`;
    badge.textContent = presentation.label;
    header.append(identity, badge);
    const progressBlock = document.createElement("div");
    progressBlock.className = "job-progress-block";
    const progressHeading = document.createElement("div");
    const stage = document.createElement("strong");
    stage.textContent = presentation.stage;
    const percent = document.createElement("span");
    const progressValue = Math.max(0, Math.min(1, Number(progressModel.fraction) || 0));
    percent.textContent = job.status === "completed"
      ? "100%" : `${Math.round(progressValue * 100)}%`;
    progressHeading.append(stage, percent);
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = job.status === "completed" ? 1 : progressValue;
    progress.setAttribute("aria-label", `${presentation.label} progress`);
    const progressText = document.createElement("p");
    progressText.className = "muted";
    const completedItems = Number(
      progressModel.completed_items ?? progressModel.completed_clusters ?? 0);
    const totalItems = Number(
      progressModel.total_items ?? progressModel.total_clusters ?? 0);
    progressText.textContent = job.kind === "style_profile"
      ? progressModel.detail
      : totalItems
        ? `${completedItems} of ${totalItems} clusters checkpointed`
        : "Waiting for the first validated checkpoint";
    progressBlock.append(progressHeading, progress, progressText);
    const detail = document.createElement("p");
    detail.className = "job-message";
    detail.textContent = job.message;

    const facts = document.createElement("div");
    facts.className = "job-facts";
    const jobFacts = job.kind === "style_profile" ? [
      ["Task", "Personal style"],
      ["Examples", stylePhotoCount || "Folder selection"],
      ["Model", job.requested_model || "Provider default"],
      ["Added", jobTime(job.created_at)],
    ] : [
      ["Profile", job.profile || "Semantic review"],
      ["Keep", job.kind === "semantic_verification" ? "Triplet judgment" : `At most ${job.keep_per_group}`],
      ["Provider", job.provider_profile_name || "Configured route"],
      ...(job.kind === "culling" ? [["Judgment", job.judgment_policy
        ? `${job.judgment_policy.panel.join(" + ")} · ${job.judgment_policy.required} of ${job.judgment_policy.votes}`
        : "Default policy"]] : []),
      ["Added", jobTime(job.created_at)],
    ];
    for (const [label, value] of jobFacts) {
      const fact = document.createElement("span");
      const factLabel = document.createElement("small");
      factLabel.textContent = label;
      const factValue = document.createElement("strong");
      factValue.textContent = String(value);
      fact.append(factLabel, factValue);
      facts.append(fact);
    }

    const details = document.createElement("details");
    details.className = "job-details";
    const detailsSummary = document.createElement("summary");
    detailsSummary.textContent = "Technical details";
    const metadata = document.createElement("dl");
    metadata.className = "job-metadata";
    for (const [label, value] of [
      ["Output", job.output],
      ["Recursive", job.kind === "semantic_verification" ? "—" : job.recursive ? "yes" : "no"],
      ["Privacy", job.provider_privacy || "declared-in-agents.kim"],
      ["Agent SHA", job.provider_config_sha256 || "legacy source"],
      ["Started", jobTime(job.started_at)],
      ["Finished", jobTime(job.finished_at)],
      ["Exit", job.exit_code ?? "—"],
    ]) {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = String(value);
      metadata.append(term, description);
    }
    details.append(detailsSummary, metadata);
    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (job.status === "running") {
      actions.append(
        jobActionButton(job, "Pause safely", "pause"),
        jobActionButton(job, "Cancel", "cancel", "danger-button"),
      );
    } else if (job.status === "queued") {
      actions.append(jobActionButton(job, "Remove from run", "cancel"));
    } else if (["paused", "cancelled"].includes(job.status)) {
      actions.append(jobActionButton(job, "Resume checkpoint", "resume", "primary-button"));
    } else if (job.status === "failed") {
      actions.append(jobActionButton(job, "Retry from checkpoint", "retry", "primary-button"));
    }
    if (job.status === "completed") {
      if (job.kind === "style_profile") {
        const inspect = document.createElement("button");
        inspect.type = "button";
        inspect.className = "quiet-button";
        inspect.textContent = "Show used examples";
        inspect.addEventListener("click", () => openStyleExtractionExamples(job));
        const useProfile = document.createElement("button");
        useProfile.type = "button";
        useProfile.className = "primary-button";
        useProfile.textContent = "Use this style profile";
        useProfile.addEventListener("click", async () => {
          try {
            state.project = await operationalPost("/api/project", {
              active_style_profile: job.output, stage: "style",
            });
            setJobFormStatus("Personal style profile selected for this project.", "success");
            renderShortlistWorkspace();
          } catch (error) {
            setJobFormStatus(error.message, "error");
          }
        });
        actions.append(useProfile, inspect);
      }
      if (job.kind !== "style_profile") {
        const open = document.createElement("button");
      open.type = "button";
      open.className = "primary-button";
      open.textContent = "Open review in new tab";
      open.addEventListener("click", async () => {
        try {
          const result = await operationalPost("/api/jobs/open-review", {
            job_id: job.id,
          });
          setJobFormStatus(result.message, "success");
        } catch (error) {
          setJobFormStatus(error.message, "error");
        }
      });
        const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "primary-button";
      copy.textContent = "Copy review command";
      copy.addEventListener("click", async () => {
        const command = `python gui.py ${JSON.stringify(job.output)} ${JSON.stringify(job.photos)}`;
        await navigator.clipboard.writeText(command);
        setJobFormStatus(
          "Review command copied. Run it for a separate review tab/server.",
          "success",
        );
      });
        actions.append(open, copy);
      }
    }
    const logs = document.createElement("details");
    logs.className = "job-log-details";
    const summary = document.createElement("summary");
    summary.textContent = job.status === "running" ? "Live activity log" : "Activity log";
    const pre = document.createElement("pre");
    pre.className = "job-log";
    pre.textContent = job.log_tail || "No output yet.";
    logs.append(summary, pre);
    const footer = document.createElement("div");
    footer.className = "job-card-footer";
    const queuePosition = document.createElement("span");
    queuePosition.className = "muted";
    const waitingBefore = visibleJobs.slice(0, jobIndex).filter(
      (candidate) => candidate.status === "queued").length;
    queuePosition.textContent = job.status === "queued"
      ? `Queue position ${waitingBefore + (active ? 2 : 1)}`
      : `Job ${job.id}`;
    footer.append(queuePosition, actions);
    article.append(
      header, progressBlock, detail, facts, details, logs, footer);
    target.append(article);
  });
  if (!visibleJobs.length) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    const mark = document.createElement("span");
    mark.textContent = "◎";
    mark.setAttribute("aria-hidden", "true");
    const heading = document.createElement("strong");
    heading.textContent = "The queue is ready";
    const message = document.createElement("p");
    message.textContent =
      "Choose a photo folder above. Darkimiya will validate it before starting.";
    empty.append(mark, heading, message);
    target.append(empty);
  }
}

function setJobFormStatus(message, kind = "") {
  const target = $("#job-form-status");
  target.textContent = message;
  target.className = `job-form-status ${kind}`.trim();
}

function styleProfileProgress(job) {
  if (job.status === "completed") {
    return {
      fraction: 1, completed_items: 1, total_items: 1,
      stage: "Profile extraction completed",
      detail: "Profile JSON committed and ready to review",
    };
  }
  if (job.status === "queued") {
    return {
      fraction: 0, completed_items: 0, total_items: 1,
      stage: "Waiting for the profile worker",
      detail: "The extraction will start when earlier queue jobs finish",
    };
  }
  const marker = /STYLE_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)/g;
  let latest = null;
  for (const match of String(job.log_tail || "").matchAll(marker)) {
    latest = {
      fraction: Math.max(0, Math.min(1, Number(match[1]) / 100)),
      stage: match[2].trim(),
    };
  }
  if (!latest) {
    latest = {fraction: .03, stage: "Starting the profile extractor"};
  }
  return {
    ...latest,
    completed_items: 0,
    total_items: 1,
    detail: "The latest validated extraction stage is shown above",
  };
}

function jobJudgmentPolicy() {
  return {
    panel: [...document.querySelectorAll(".job-judge-agent:checked")]
      .map((input) => input.value),
    votes: Number($("#job-judge-votes").value),
    required: Number($("#job-judge-required").value),
  };
}

function updateJobReadiness() {
  const photos = $("#job-photos").value.trim();
  const keepers = Number($("#job-keepers").value);
  const judgment = jobJudgmentPolicy();
  const judgmentValid = judgment.panel.length > 0 &&
    Number.isInteger(judgment.votes) && judgment.votes >= judgment.panel.length &&
    judgment.votes <= 15 && Number.isInteger(judgment.required) &&
    judgment.required >= 1 && judgment.required <= judgment.votes;
  const customJudgment = judgment.votes !== 5 || judgment.required !== 4 ||
    judgment.panel.join(",") !== "C,D";
  const providerReady = !customJudgment || Boolean($("#job-provider").value);
  const ready = Boolean(photos) && Number.isInteger(keepers) &&
    keepers >= 1 && keepers <= 20 && judgmentValid && providerReady;
  $("#add-job").disabled = !ready;
  if (!photos) {
    setJobFormStatus("Choose a photo folder to begin.");
  } else if (!Number.isInteger(keepers) || keepers < 1 || keepers > 20) {
    setJobFormStatus("Maximum per group must be between 1 and 20.", "error");
  } else if (!judgment.panel.length) {
    setJobFormStatus("Select at least one judgment-panel agent.", "error");
  } else if (!judgmentValid) {
    setJobFormStatus(
      "Use 1–15 votes, at least one per panel agent, with approvals no greater than votes.",
      "error",
    );
  } else if (!providerReady) {
    setJobFormStatus(
      "Choose a provider profile before using a custom judgment policy.",
      "error",
    );
  } else {
    setJobFormStatus(
      "Ready to validate. The job configuration will be fixed when queued.",
      "ready",
    );
  }
}

async function refreshJobs(schedule = false) {
  try {
    const response = await fetch("/api/jobs");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load jobs");
    state.jobs = payload;
    if (!state.styleExtractionJobId) {
      const latestStyleJob = [...payload.jobs].reverse().find(
        (job) => job.kind === "style_profile" &&
          ["queued", "running", "stopping", "detached", "failed", "completed"].includes(job.status));
      state.styleExtractionJobId = latestStyleJob?.id || null;
    }
    if (!state.rendererExportJobId) {
      const latestRendererExportJob = [...payload.jobs].reverse().find(
        (job) => job.kind === "renderer_export" &&
          ["queued", "running", "stopping", "detached"].includes(job.status));
      state.rendererExportJobId = latestRendererExportJob?.id || null;
    }
    if (!state.deliveryExportJobId) {
      const activeDelivery = [...payload.jobs].reverse().find(
        (job) => job.kind === "delivery_export" &&
          ["queued", "running", "stopping", "detached"].includes(job.status));
      state.deliveryExportJobId = activeDelivery?.id || null;
    }
    renderJobs();
    renderStyleExtractionProgress();
    renderDevelopmentJobStatus();
    renderComparisonJobStatus();
    renderRendererExportJobStatus();
    renderDeliveryExportJobStatus();
    populateStyleProfileOptions();
  } catch (error) {
    setJobFormStatus(error.message, "error");
  }
  clearTimeout(state.jobPoll);
  const styleJob = state.jobs?.jobs?.find(
    (job) => job.id === state.styleExtractionJobId);
  const styleActive = styleJob && ["queued", "running", "stopping", "detached"].includes(styleJob.status);
  const developmentJob = state.jobs?.jobs?.find(
    (job) => job.id === state.developmentJobId);
  const developmentActive = developmentJob &&
    ["queued", "running", "stopping", "detached"].includes(developmentJob.status);
  const comparisonJob = state.jobs?.jobs?.find(
    (job) => job.id === state.comparisonJobId);
  const comparisonActive = comparisonJob &&
    ["queued", "running", "stopping", "detached"].includes(comparisonJob.status);
  const rendererExportJob = state.jobs?.jobs?.find(
    (job) => job.id === state.rendererExportJobId);
  const rendererExportActive = rendererExportJob &&
    ["queued", "running", "stopping", "detached"].includes(rendererExportJob.status);
  const deliveryExportJob = state.jobs?.jobs?.find(
    (job) => job.id === state.deliveryExportJobId);
  const deliveryExportActive = deliveryExportJob &&
    ["queued", "running", "stopping", "detached"].includes(deliveryExportJob.status);
  if (schedule && ($("#jobs-dialog").open ||
      (styleActive && ($("#style-profile-dialog").open || state.workspaceMode === "style")) ||
      ((developmentActive || comparisonActive || rendererExportActive || deliveryExportActive) &&
        state.workspaceMode === "develop"))) {
    state.jobPoll = setTimeout(() => refreshJobs(true), 1000);
  }
}

async function addJob() {
  setJobFormStatus("Validating folder, output, provider, and Kimiya program…", "working");
  $("#add-job").disabled = true;
  try {
    const judgment = jobJudgmentPolicy();
    state.jobs = await operationalPost("/api/jobs/add", {
      photos: $("#job-photos").value.trim(),
      output: $("#job-output").value.trim(),
      keep_per_group: Number($("#job-keepers").value),
      recursive: $("#job-recursive").checked,
      profile: $("#job-profile").value,
      provider_profile_id: $("#job-provider").value,
      judge_panel: judgment.panel,
      judge_votes: judgment.votes,
      judge_required: judgment.required,
    });
    setJobFormStatus(
      "Folder validated and added. Sequential processing starts automatically.",
      "success",
    );
    $("#job-photos").value = "";
    $("#job-output").value = "";
    $("#add-job").disabled = true;
    renderJobs();
  } catch (error) {
    setJobFormStatus(error.message, "error");
    $("#add-job").disabled = false;
  }
}

function providerDefaults(kind) {
  if (kind === "openrouter") return {
    endpoint: "https://openrouter.ai/api/v1",
    credential: true,
    zdr: true,
    models: {
      A: "openai/gpt-5.6-luna-pro",
      B: "openai/gpt-5.6-luna-pro",
      C: "openai/gpt-4.1-mini",
      D: "openai/gpt-5.6-luna-pro",
    },
  };
  if (kind === "ollama") return {
    endpoint: "http://127.0.0.1:11434",
    credential: false,
    zdr: false,
    models: {
      A: "qwen2.5vl:7b", B: "qwen2.5vl:7b",
      C: "llama3.1:8b", D: "qwen2.5vl:7b",
    },
  };
  return {
    endpoint: "https://YOUR-POD.example/v1",
    credential: false,
    zdr: false,
    models: {
      A: "Qwen/Qwen2.5-VL-7B-Instruct",
      B: "Qwen/Qwen2.5-VL-7B-Instruct",
      C: "mistralai/Mistral-7B-Instruct-v0.3",
      D: "Qwen/Qwen2.5-VL-7B-Instruct",
    },
  };
}

function providerKindLabel(kind) {
  return kind === "openrouter" ? "OpenRouter"
    : kind === "ollama" ? "Local Ollama" : "OpenAI-compatible";
}

function providerOptionLabel(profile) {
  return `${profile.name} · ${providerKindLabel(profile.kind)} · ${profile.models.A}`;
}

function providerTrustPresentation(kind, zdr, endpoint) {
  let localEndpoint = false;
  try {
    localEndpoint = ["127.0.0.1", "localhost"].includes(
      new URL(endpoint || "http://invalid").hostname);
  } catch (_) {
    localEndpoint = false;
  }
  if (kind === "ollama" && localEndpoint) return {
    privacy: "local",
    label: "Local processing",
    tone: "local",
    icon: "⌂",
    title: "Photographs stay on this Mac",
    description:
      "All four agents use the local Ollama endpoint. No model request is routed to a remote provider.",
  };
  if (kind === "openrouter" && zdr) return {
    privacy: "remote-zdr",
    label: "Remote · ZDR required",
    tone: "zdr",
    icon: "◇",
    title: "Remote processing with ZDR enforcement",
    description:
      "Observed previews leave this Mac. Darkimiya requires OpenRouter Zero Data Retention routing for every configured agent.",
  };
  return {
    privacy: "remote-provider-policy",
    label: "Remote provider policy",
    tone: "remote",
    icon: "↗",
    title: "Remote processing uses the endpoint’s policy",
    description:
      "Observed previews leave this Mac for vision roles. Review the endpoint operator’s retention, security, and cost terms.",
  };
}

function setProviderStatus(message, kind = "") {
  const target = $("#provider-status");
  target.textContent = message;
  target.className = `provider-status ${kind}`.trim();
}

function selectedProviderProfile() {
  const id = $("#provider-id").value;
  return state.providers?.profiles?.find((profile) => profile.id === id) || null;
}

function renderProviderTrust(profile = selectedProviderProfile()) {
  const kind = $("#provider-kind").value;
  const presentation = providerTrustPresentation(
    kind,
    $("#provider-zdr").checked,
    kind === "openrouter"
      ? "https://openrouter.ai/api/v1" : $("#provider-endpoint").value,
  );
  const privacy = $("#provider-privacy-badge");
  privacy.textContent = presentation.label;
  privacy.className = `provider-trust-badge ${presentation.tone}`;
  $("#provider-trust-icon").textContent = presentation.icon;
  $("#provider-trust-icon").className = presentation.tone;
  $("#provider-routing-title").textContent = presentation.title;
  $("#provider-routing-description").textContent = presentation.description;

  const secretEntered = Boolean($("#provider-secret").value);
  const credentialState = secretEntered
    ? "New credential ready"
    : profile?.credential === "stored"
      ? "Credential stored"
      : $("#provider-credential-required").checked
        ? "Credential missing"
        : "No credential required";
  const credentialTone = secretEntered || profile?.credential === "stored"
    ? "stored" : $("#provider-credential-required").checked ? "missing" : "optional";
  const credential = $("#provider-credential-badge");
  credential.textContent = credentialState;
  credential.className = `provider-trust-badge ${credentialTone}`;
  $("#provider-secret-help").textContent = profile?.credential === "stored"
    ? "A credential is stored. Its value cannot be displayed; leave this blank to keep it."
    : $("#provider-credential-required").checked
      ? "A credential must be supplied before this profile can be saved."
      : "Optional credentials are still stored only in macOS Keychain.";
  $("#provider-editor-title").textContent =
    $("#provider-name").value.trim() || "New provider profile";
}

function updateProviderKind(useDefaults = false) {
  const kind = $("#provider-kind").value;
  const defaults = providerDefaults(kind);
  $(".provider-endpoint").hidden = kind === "openrouter";
  $("#provider-zdr").closest("label").hidden = kind !== "openrouter";
  $("#provider-credential-required").disabled = kind === "openrouter";
  if (useDefaults) {
    $("#provider-endpoint").value = defaults.endpoint;
    $("#provider-credential-required").checked = defaults.credential;
    $("#provider-zdr").checked = defaults.zdr;
    for (const agent of ["a", "b", "c", "d"]) {
      $(`#provider-model-${agent}`).value = defaults.models[agent.toUpperCase()];
    }
  }
  renderProviderTrust();
}

function populateProviderSelect() {
  const select = $("#job-provider");
  const selected = select.value;
  select.replaceChildren();
  const profiles = state.providers?.profiles || [];
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent =
      `${profile.name} · ${profile.privacy} · credential ${profile.credential}`;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === selected)) {
    select.value = selected;
  } else if (profiles.length) {
    select.value = profiles[0].id;
  } else {
    const unavailable = document.createElement("option");
    unavailable.value = "";
    unavailable.textContent = "Configure a provider first";
    select.append(unavailable);
  }
  updateJobReadiness();
}

function editProvider(profile = null) {
  const kind = profile?.kind || "openrouter";
  const defaults = providerDefaults(kind);
  $("#provider-id").value = profile?.id || "";
  $("#provider-name").value = profile?.name || "";
  $("#provider-kind").value = kind;
  $("#provider-endpoint").value = profile?.endpoint || defaults.endpoint;
  for (const agent of ["a", "b", "c", "d"]) {
    $(`#provider-model-${agent}`).value =
      profile?.models?.[agent.toUpperCase()] || defaults.models[agent.toUpperCase()];
  }
  $("#provider-credential-required").checked =
    profile?.credential_required ?? defaults.credential;
  $("#provider-zdr").checked = profile?.zdr ?? defaults.zdr;
  $("#provider-secret").value = "";
  $("#provider-cost-note").value = profile?.cost_note || "";
  $("#test-provider").disabled = !profile;
  $("#delete-provider").disabled = !profile;
  setProviderStatus(profile
    ? "Saved profile loaded. Changes affect only jobs queued after the next save."
    : "Create a route, assign all four agent models, then save it.");
  $("#provider-test-result").hidden = true;
  updateProviderKind(false);
  renderProviderTrust(profile);
  renderProviders();
}

function renderProviders() {
  const target = $("#provider-list");
  target.replaceChildren();
  const profiles = state.providers?.profiles || [];
  $("#provider-local-count").textContent = String(
    profiles.filter((profile) => profile.privacy === "local").length);
  $("#provider-zdr-count").textContent = String(
    profiles.filter((profile) => profile.privacy === "remote-zdr").length);
  $("#provider-attention-count").textContent = String(
    profiles.filter((profile) =>
      profile.credential_required && profile.credential !== "stored").length);
  const activeId = $("#provider-id").value;
  for (const profile of profiles) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "provider-item";
    if (profile.id === activeId) {
      button.classList.add("active");
      button.setAttribute("aria-current", "true");
    }
    const top = document.createElement("span");
    top.className = "provider-item-top";
    const mark = document.createElement("span");
    mark.className = `provider-kind-mark ${profile.privacy}`;
    mark.textContent = profile.kind === "ollama"
      ? "⌂" : profile.kind === "openrouter" ? "◇" : "↗";
    const name = document.createElement("strong");
    name.textContent = profile.name;
    const privacy = document.createElement("span");
    privacy.className = `provider-item-privacy ${profile.privacy}`;
    privacy.textContent = profile.privacy === "local"
      ? "Local" : profile.privacy === "remote-zdr" ? "Remote · ZDR" : "Remote";
    top.append(mark, name, privacy);
    const detail = document.createElement("span");
    detail.className = "provider-item-detail";
    detail.textContent = profile.credential === "stored"
      ? "Credential protected by Keychain"
      : profile.credential_required ? "Credential required" : "No credential required";
    button.append(top, detail);
    button.addEventListener("click", () => editProvider(profile));
    target.append(button);
  }
  if (!profiles.length) {
    const empty = document.createElement("div");
    empty.className = "provider-list-empty";
    empty.textContent = "No saved routes yet.";
    target.append(empty);
  }
  populateProviderSelect();
}

async function refreshProviders() {
  try {
    const response = await fetch("/api/providers");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load providers");
    state.providers = payload;
    renderProviders();
  } catch (error) {
    setProviderStatus(error.message, "error");
  }
}

async function saveProvider() {
  setProviderStatus("Saving route metadata and Keychain credential reference…", "working");
  const kind = $("#provider-kind").value;
  try {
    state.providers = await operationalPost("/api/providers/save", {
      revision: state.providers.revision,
      profile: {
        id: $("#provider-id").value,
        name: $("#provider-name").value.trim(),
        kind,
        endpoint: $("#provider-endpoint").value.trim(),
        models: {
          A: $("#provider-model-a").value.trim(),
          B: $("#provider-model-b").value.trim(),
          C: $("#provider-model-c").value.trim(),
          D: $("#provider-model-d").value.trim(),
        },
        credential_required: $("#provider-credential-required").checked,
        zdr: $("#provider-zdr").checked,
        cost_note: $("#provider-cost-note").value.trim(),
      },
      secret: $("#provider-secret").value,
    });
    $("#provider-secret").value = "";
    renderProviders();
    const saved = state.providers.profiles.find(
      (profile) => profile.name === $("#provider-name").value.trim());
    editProvider(saved || null);
    setProviderStatus(
      "Profile saved. The credential value was not returned to this interface.",
      "success",
    );
  } catch (error) {
    $("#provider-secret").value = "";
    renderProviderTrust();
    setProviderStatus(error.message, "error");
  }
}

function renderProviderTestResult(result) {
  const target = $("#provider-test-result");
  target.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "provider-test-heading";
  const title = document.createElement("strong");
  title.textContent = result.configured_models_present
    ? "Route validation passed" : "Route reachable with configuration issues";
  const count = document.createElement("span");
  count.textContent = `${result.available_model_count} models reported`;
  heading.append(title, count);
  const checks = document.createElement("div");
  checks.className = "provider-test-checks";
  for (const [label, passed, detail] of [
    ["Endpoint", result.reachable, result.reachable ? "Reachable" : "Unavailable"],
    [
      "Model IDs",
      result.configured_models_present,
      result.configured_models_present
        ? "All configured IDs found"
        : `${result.missing_models.length} missing`,
    ],
  ]) {
    const check = document.createElement("div");
    check.className = passed ? "passed" : "failed";
    const icon = document.createElement("span");
    icon.textContent = passed ? "✓" : "!";
    const text = document.createElement("span");
    const checkTitle = document.createElement("strong");
    checkTitle.textContent = label;
    const checkDetail = document.createElement("small");
    checkDetail.textContent = detail;
    text.append(checkTitle, checkDetail);
    check.append(icon, text);
    checks.append(check);
  }
  const roles = document.createElement("div");
  roles.className = "provider-vision-results";
  for (const agent of ["A", "B", "C", "D"]) {
    const row = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = `Agent ${agent}`;
    const status = document.createElement("span");
    status.textContent = String(result.vision_capability?.[agent] || "unknown")
      .replaceAll("-", " ");
    row.append(label, status);
    roles.append(row);
  }
  target.append(heading, checks, roles);
  if (result.missing_models?.length) {
    const missing = document.createElement("p");
    missing.className = "provider-missing-models";
    missing.textContent = `Missing: ${result.missing_models.join(", ")}`;
    target.append(missing);
  }
  const notice = document.createElement("p");
  notice.className = "provider-test-notice";
  notice.textContent = result.notice;
  target.append(notice);
  target.hidden = false;
}

async function testProvider() {
  const profileId = $("#provider-id").value;
  if (!profileId) return;
  setProviderStatus("Testing endpoint, model IDs, and declared vision support…", "working");
  $("#provider-test-result").hidden = true;
  try {
    const result = await operationalPost("/api/providers/test", {
      profile_id: profileId,
    });
    renderProviderTestResult(result);
    setProviderStatus(result.configured_models_present
      ? "Endpoint reachable and every configured model ID was found."
      : "Endpoint reachable, but one or more configured model IDs were not found.",
    result.configured_models_present ? "success" : "warning");
  } catch (error) {
    setProviderStatus(error.message, "error");
    const target = $("#provider-test-result");
    target.replaceChildren();
    const failure = document.createElement("div");
    failure.className = "provider-test-failure";
    const title = document.createElement("strong");
    title.textContent = "Connection test failed";
    const message = document.createElement("p");
    message.textContent = error.message;
    failure.append(title, message);
    target.append(failure);
    target.hidden = false;
  }
}

async function deleteProvider() {
  const profileId = $("#provider-id").value;
  if (!profileId) return;
  const removeCredential = window.confirm(
    "Also remove this profile’s credential from macOS Keychain?");
  if (!window.confirm(
    "Delete this profile? Already queued jobs keep their immutable generated configuration.")) return;
  try {
    state.providers = await operationalPost("/api/providers/delete", {
      profile_id: profileId,
      revision: state.providers.revision,
      remove_credential: removeCredential,
    });
    renderProviders();
    editProvider(null);
    setProviderStatus(
      "Profile deleted. Previously queued jobs retain their immutable configuration.",
      "success",
    );
  } catch (error) {
    setProviderStatus(error.message, "error");
  }
}

async function pickJobFolder() {
  setJobFormStatus("Waiting for macOS folder selection…", "working");
  try {
    const result = await operationalPost("/api/jobs/pick-folder", {});
    $("#job-photos").value = result.photos;
    updateJobReadiness();
  } catch (error) {
    setJobFormStatus(error.message, "error");
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
    photo_annotations: structuredClone(existing?.photo_annotations || {}),
  };
  state.drafts.set(cluster.cluster_id, draft);
  return draft;
}

function flags(cluster) {
  const decision = decisionFor(cluster);
  const human = state.drafts.get(cluster.cluster_id)
    || humanReview(cluster.cluster_id);
  const reviewed = Boolean(human && human.reviewed);
  const annotations = Object.values(human?.photo_annotations || {});
  return {
    selected: decision.photos.length > 0,
    warning: Boolean(decision.warning),
    fallback: Boolean(decision.fallback),
    empty: decision.photos.length === 0,
    oversized: cluster.photos.length > 8,
    unreviewed: !reviewed,
    reviewed,
    modified: reviewed && !arraysEqual(human.keepers, decision.photos),
    maybe: annotations.some((item) => item.flag === "maybe"),
    rejected: annotations.some((item) => item.flag === "reject"),
    lowconfidence: Number(decision.confidence) < 0.65,
  };
}

function renderSummary() {
  const summary = state.payload.summary;
  const reviewStatus = state.review.status;
  const target = $("#summary");
  $("#undo-review").disabled =
    !state.review.status.compatible || !state.review.history.length;
  target.replaceChildren();
  const reviewed = Number(reviewStatus.reviewed_clusters || 0);
  const total = Number(summary.clusters || 0);
  const remaining = Math.max(0, total - reviewed);
  const copy = document.createElement("p");
  copy.className = "project-progress-copy";
  copy.textContent = `${summary.photos} photos · ${total} groups · ` +
    `${reviewed} reviewed · ${remaining} remaining`;
  const progress = document.createElement("progress");
  progress.className = "project-review-progress";
  progress.max = Math.max(1, total);
  progress.value = reviewed;
  progress.setAttribute("aria-label", `${reviewed} of ${total} groups reviewed`);
  target.append(copy, progress);
  $("#report-status").textContent = `${reviewed} of ${total} reviewed`;
}

function formatDuration(seconds) {
  if (!seconds) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function clusterMatches(cluster) {
  const decision = decisionFor(cluster);
  const clusterFlags = flags(cluster);
  if (state.activeFilter !== "all" && !clusterFlags[state.activeFilter]) return false;
  const personId = $("#person-filter")?.value || "";
  if (personId && !cluster.photos.some(
    (name) => (state.people?.photo_people?.[name] || []).includes(personId)
  )) return false;
  if (!state.query) return true;
  const haystack = [
    cluster.cluster_id,
    ...cluster.photos,
    ...decision.photos,
    decision.rationale || "",
  ].join(" ").toLowerCase();
  return haystack.includes(state.query.toLowerCase());
}

function populatePersonFilter() {
  const select = $("#person-filter");
  const selected = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All people";
  select.append(all);
  for (const person of state.people?.people || []) {
    const option = document.createElement("option");
    option.value = person.id;
    option.textContent =
      `${person.display_name} · ${person.photo_count} photo${person.photo_count === 1 ? "" : "s"}`;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === selected)) {
    select.value = selected;
  }
}

function activePerson() {
  return state.people?.people.find(
    (person) => person.id === state.activePersonId) || null;
}

function setPersonActionStatus(message, kind = "") {
  const target = $("#person-action-status");
  target.textContent = message;
  target.className = `person-action-status ${kind}`.trim();
}

function updateMergeSelection() {
  const count = state.mergeSelection.size;
  $("#people-merge-count").textContent = count
    ? `${count} group${count === 1 ? "" : "s"} selected`
    : "No groups selected";
  $("#merge-people").disabled = count < 2;
}

function updateFaceSelection() {
  const count = state.faceSelection.size;
  $("#person-face-selection").textContent = count
    ? `${count} face${count === 1 ? "" : "s"} selected`
    : "No faces selected";
  $("#split-person").disabled = count === 0;
}

function renderPersonDetail() {
  const person = activePerson();
  $("#person-empty").hidden = Boolean(person);
  $("#person-content").hidden = !person;
  if (!person) return;
  $("#person-id").textContent = person.id;
  $("#person-title").textContent = person.display_name;
  $("#person-name").value = person.private_name;
  $("#person-confirmed").checked = person.confirmed;
  const confirmation = $("#person-confirmation-badge");
  confirmation.textContent = person.confirmed ? "Identity confirmed" : "Needs review";
  confirmation.className =
    `person-confirmation-badge ${person.confirmed ? "confirmed" : "unconfirmed"}`;
  $("#person-face-total").textContent = String(person.face_count);
  $("#person-photo-total").textContent = String(person.photo_count);
  $("#person-selected-total").textContent =
    String(person.coverage.selected_photos);
  $("#person-unselected-total").textContent =
    String(person.coverage.unselected_photos);
  $("#person-coverage").textContent =
    `${person.face_count} faces in ${person.photo_count} photos · ` +
    `${person.coverage.selected_photos} selected · ` +
    `${person.coverage.unselected_photos} not selected`;
  const target = $("#person-faces");
  target.replaceChildren();
  if (!state.personFacePages.has(person.id)) {
    state.personFacePages.set(person.id, [...person.faces]);
  }
  const faces = state.personFacePages.get(person.id);
  for (const face of faces) {
    const label = document.createElement("label");
    label.className = "person-face";
    const image = document.createElement("img");
    image.loading = "lazy";
    image.alt = `${person.display_name} in ${face.photo_name}`;
    image.src = `/api/face?id=${encodeURIComponent(face.id)}`;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.faceId = face.id;
    checkbox.checked = state.faceSelection.has(face.id);
    checkbox.setAttribute("aria-label", `Select ${face.photo_name} for splitting`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.faceSelection.add(face.id);
      else state.faceSelection.delete(face.id);
      label.classList.toggle("selected", checkbox.checked);
      updateFaceSelection();
    });
    if (checkbox.checked) label.classList.add("selected");
    const confidence = document.createElement("span");
    const clusterConfidence = Math.round(face.cluster_confidence * 100);
    confidence.className = `face-confidence ${
      clusterConfidence >= 75 ? "high" : clusterConfidence >= 55 ? "medium" : "low"}`;
    confidence.textContent = `${clusterConfidence}% group`;
    const caption = document.createElement("span");
    caption.className = "person-face-caption";
    const filename = document.createElement("strong");
    filename.textContent = face.photo_name;
    const detection = document.createElement("small");
    detection.textContent =
      `${Math.round(face.detection_confidence * 100)}% detection confidence`;
    caption.append(filename, detection);
    label.append(image, checkbox, confidence, caption);
    target.append(label);
  }
  const loadMore = $("#load-more-faces");
  loadMore.hidden = faces.length >= person.face_count;
  loadMore.textContent =
    `Load more faces (${faces.length} of ${person.face_count})`;
  updateFaceSelection();
}

function renderPeople() {
  if (!state.people) return;
  const status = state.people.status;
  $("#people-activity").textContent = status.state === "running"
    ? `${status.processed} / ${status.total}` : status.state === "failed"
      ? "Needs attention" : "Idle";
  $("#people-activity").className =
    status.state === "failed" ? "attention" :
      status.state === "running" ? "active" : "";
  $("#people-privacy-badge").textContent = "Local only";
  $("#people-privacy-badge").className = "people-privacy-badge";
  const knownPeople = new Set(state.people.people.map((person) => person.id));
  state.mergeSelection = new Set(
    [...state.mergeSelection].filter((id) => knownPeople.has(id)));
  const stateLabels = {
    idle: "Ready for local indexing",
    running: "Indexing photographs locally",
    paused: "Indexing paused safely",
    completed: "Local index is up to date",
    failed: "Local indexing needs attention",
  };
  const effectiveState = status.state === "idle"
    && status.total > 0
    && status.processed === status.total
    ? "completed"
    : status.state;
  $("#people-index-status").textContent =
    stateLabels[effectiveState] || String(effectiveState);
  $("#people-privacy-status").textContent =
    `Pinned local models · embeddings hidden` +
    (status.error ? ` · ${status.error}` : "");
  const fraction = status.total ? status.processed / status.total : 0;
  $("#people-progress").value = fraction;
  $("#people-progress-percent").textContent = `${Math.round(fraction * 100)}%`;
  $("#people-progress-label").textContent =
    `${status.processed} of ${status.total} photographs`;
  $("#people-group-count").textContent = String(status.people);
  $("#people-face-count").textContent = String(status.faces);
  const confirmed = state.people.people.filter((person) => person.confirmed).length;
  $("#people-confirmed-count").textContent = String(confirmed);
  $("#people-review-count").textContent =
    String(state.people.people.length - confirmed);
  $("#start-people-index").disabled = status.state === "running";
  $("#pause-people-index").disabled = status.state !== "running";
  const list = $("#people-list");
  list.replaceChildren();
  const query = state.peopleQuery.toLowerCase();
  const visible = state.people.people.filter((person) => {
    if (state.peopleFilter === "confirmed" && !person.confirmed) return false;
    if (state.peopleFilter === "unconfirmed" && person.confirmed) return false;
    return !query || `${person.display_name} ${person.id}`.toLowerCase().includes(query);
  });
  $("#people-visible-count").textContent =
    `${visible.length} of ${state.people.people.length}`;
  for (const person of visible) {
    const row = document.createElement("div");
    row.className = "person-row";
    if (person.id === state.activePersonId) row.classList.add("active");
    const merge = document.createElement("input");
    merge.type = "checkbox";
    merge.dataset.personMerge = person.id;
    merge.title = "Check for merge";
    merge.checked = state.mergeSelection.has(person.id);
    merge.setAttribute("aria-label", `Select ${person.display_name} for merging`);
    merge.addEventListener("change", () => {
      if (merge.checked) state.mergeSelection.add(person.id);
      else state.mergeSelection.delete(person.id);
      updateMergeSelection();
    });
    const button = document.createElement("button");
    button.type = "button";
    const avatar = document.createElement("span");
    avatar.className = "person-list-avatar";
    if (person.faces[0]) {
      const image = document.createElement("img");
      image.alt = "";
      image.src = `/api/face?id=${encodeURIComponent(person.faces[0].id)}`;
      avatar.append(image);
    } else {
      avatar.textContent = "◎";
    }
    const identity = document.createElement("span");
    identity.className = "person-list-identity";
    const title = document.createElement("strong");
    title.textContent = person.display_name;
    const detail = document.createElement("span");
    detail.textContent =
      `${person.face_count} faces · ${person.photo_count} photos`;
    identity.append(title, detail);
    const badge = document.createElement("span");
    badge.className = `person-list-state ${person.confirmed ? "confirmed" : ""}`;
    badge.textContent = person.confirmed ? "Confirmed" : "Review";
    button.append(avatar, identity, badge);
    button.addEventListener("click", () => {
      state.activePersonId = person.id;
      state.faceSelection.clear();
      renderPeople();
    });
    row.append(merge, button);
    list.append(row);
  }
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "people-list-empty";
    empty.textContent = state.people.people.length
      ? "No groups match this filter." : "No face groups indexed yet.";
    list.append(empty);
  }
  document.querySelectorAll(".people-filter").forEach((button) => {
    const active = button.dataset.peopleFilter === state.peopleFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  updateMergeSelection();
  populatePersonFilter();
  renderPersonDetail();
  renderClusterList();
}

async function refreshPeople(schedule = false) {
  try {
    const response = await fetch("/api/people");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load people");
    state.people = payload;
    if (
      state.activePersonId
      && !state.people.people.some((person) => person.id === state.activePersonId)
    ) state.activePersonId = null;
    renderPeople();
  } catch (error) {
    $("#people-privacy-status").textContent = error.message;
    $("#people-privacy-badge").textContent = "Index unavailable";
    $("#people-privacy-badge").className = "people-privacy-badge error";
  }
  clearTimeout(state.personPoll);
  if (
    schedule && $("#people-dialog").open
    && state.people?.status?.state === "running"
  ) state.personPoll = setTimeout(() => refreshPeople(true), 600);
}

async function peopleAction(path, body = {}) {
  try {
    state.people = await operationalPost(path, body);
    state.personFacePages.clear();
    renderPeople();
    return true;
  } catch (error) {
    setPersonActionStatus(error.message, "error");
    return false;
  }
}

async function loadMoreFaces() {
  const person = activePerson();
  if (!person) return;
  const current = state.personFacePages.get(person.id) || [...person.faces];
  try {
    const params = new URLSearchParams({
      id: person.id, offset: String(current.length), limit: "100",
    });
    const response = await fetch(`/api/person-faces?${params}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load faces");
    const known = new Set(current.map((face) => face.id));
    state.personFacePages.set(person.id, [
      ...current,
      ...payload.faces.filter((face) => !known.has(face.id)),
    ]);
    renderPersonDetail();
  } catch (error) {
    setPersonActionStatus(error.message, "error");
  }
}

async function startPeopleIndex() {
  if (await peopleAction("/api/people/start")) refreshPeople(true);
}

async function savePerson() {
  const person = activePerson();
  if (!person) return;
  if (await peopleAction("/api/people/rename", {
    person_id: person.id,
    name: $("#person-name").value.trim(),
    confirmed: $("#person-confirmed").checked,
  })) setPersonActionStatus("Private identity saved locally.", "success");
}

async function mergePeople() {
  const ids = [...state.mergeSelection];
  if (ids.length < 2) {
    setPersonActionStatus("Select at least two compatible groups to merge.", "error");
    return;
  }
  if (await peopleAction("/api/people/merge", {person_ids: ids})) {
    state.mergeSelection.clear();
    updateMergeSelection();
    renderPeople();
    setPersonActionStatus("Selected groups merged into one private identity.", "success");
  }
}

async function splitPerson() {
  const person = activePerson();
  const faceIds = [...state.faceSelection];
  if (!person || !faceIds.length) {
    setPersonActionStatus(
      "Select the faces that should become a separate anonymous group.",
      "error",
    );
    return;
  }
  if (await peopleAction("/api/people/split", {
    person_id: person.id, face_ids: faceIds,
  })) {
    state.faceSelection.clear();
    renderPersonDetail();
    setPersonActionStatus("Selected faces moved to a new anonymous group.", "success");
  }
}

async function forgetPerson() {
  const person = activePerson();
  if (!person) return;
  const confirmation = window.prompt(
    `Delete this person’s face detections and embeddings?\nType: FORGET ${person.id}`);
  if (confirmation !== `FORGET ${person.id}`) return;
  if (await peopleAction("/api/people/forget", {
    person_id: person.id, confirmation,
  })) {
    state.activePersonId = null;
    state.faceSelection.clear();
    renderPeople();
  }
}

async function deleteAllPeople() {
  const expected = "DELETE ALL PRIVATE FACE DATA";
  const confirmation = window.prompt(
    `Delete every local face, embedding, group, and private name?\nType: ${expected}`);
  if (confirmation !== expected) return;
  if (await peopleAction("/api/people/delete-all", {confirmation})) {
    state.activePersonId = null;
    state.faceSelection.clear();
    state.mergeSelection.clear();
    renderPeople();
  }
}

function renderClusterList() {
  state.visible = state.clusters.filter(clusterMatches);
  $("#cluster-count").textContent = `${state.visible.length} of ${state.clusters.length} clusters`;
  const filterLabels = {
    all: "All clusters", selected: "AI selected", warning: "Warnings",
    fallback: "Fallback", empty: "No keeper", oversized: "Tournament",
    unreviewed: "Unreviewed", reviewed: "Human reviewed",
    modified: "Human modified", maybe: "Has maybe", rejected: "Has reject",
    lowconfidence: "Low confidence",
  };
  const personName = $("#person-filter")?.selectedOptions?.[0]?.textContent;
  $("#active-filter-label").textContent = [
    filterLabels[state.activeFilter] || "All clusters",
    $("#person-filter")?.value ? personName : "",
  ].filter(Boolean).join(" · ");
  const target = $("#cluster-list");
  target.setAttribute("aria-busy", "true");
  target.replaceChildren();
  const activeIndex = state.visible.findIndex(
    (cluster) => cluster.cluster_id === state.activeClusterId);
  const windowEnd = state.clusterWindowStart + state.clusterWindowSize;
  if (activeIndex >= 0 &&
      (activeIndex < state.clusterWindowStart || activeIndex >= windowEnd)) {
    state.clusterWindowStart = Math.max(
      0, activeIndex - Math.floor(state.clusterWindowSize / 2));
  }
  const maximumStart = Math.max(0, state.visible.length - state.clusterWindowSize);
  state.clusterWindowStart = Math.min(state.clusterWindowStart, maximumStart);
  const rendered = state.visible.slice(
    state.clusterWindowStart,
    state.clusterWindowStart + state.clusterWindowSize);
  const fragment = document.createDocumentFragment();
  rendered.forEach((cluster, windowIndex) => {
    const decision = decisionFor(cluster);
    const clusterFlags = flags(cluster);
    const human = state.drafts.get(cluster.cluster_id) || humanReview(cluster.cluster_id);
    const kept = human?.reviewed ? human.keepers.length : decision.photos.length;
    const absoluteIndex = state.clusterWindowStart + windowIndex + 1;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cluster-item";
    button.dataset.id = cluster.cluster_id;
    button.setAttribute(
      "aria-label",
      `Cluster ${absoluteIndex} of ${state.visible.length}, ` +
      `${cluster.photos.length} photographs`);
    const active = cluster.cluster_id === state.activeClusterId;
    if (active) button.classList.add("active");

    const visual = document.createElement("span");
    visual.className = "cluster-visual";
    const position = document.createElement("span");
    position.textContent = String(absoluteIndex).padStart(2, "0");
    visual.append(position);
    if (active && cluster.photos[0]) {
      const image = document.createElement("img");
      image.alt = "";
      bindPreviewImage(image, cluster.photos[0], "thumb");
      image.addEventListener("error", () => image.remove());
      visual.prepend(image);
    }

    const copy = document.createElement("span");
    copy.className = "cluster-item-copy";
    const title = document.createElement("strong");
    title.textContent = cluster.cluster_id;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${cluster.photos.length} photo${cluster.photos.length === 1 ? "" : "s"} · ${kept} kept`;
    copy.append(title, count);

    const status = document.createElement("span");
    status.className = "cluster-row-status";
    const statusDot = document.createElement("span");
    statusDot.className = "cluster-status-dot";
    let statusText = "Unreviewed";
    if (clusterFlags.warning || clusterFlags.fallback) {
      status.classList.add("attention"); statusText = "Attention";
    } else if (clusterFlags.modified) {
      status.classList.add("modified"); statusText = "Modified";
    } else if (clusterFlags.reviewed) {
      status.classList.add("reviewed"); statusText = "Reviewed";
    }
    status.title = statusText;
    const statusLabel = document.createElement("span");
    statusLabel.textContent = statusText;
    status.append(statusDot, statusLabel);
    button.append(visual, copy, status);
    button.addEventListener("click", () => showCluster(cluster.cluster_id));
    fragment.append(button);
  });
  target.append(fragment);
  target.setAttribute("aria-busy", "false");
  const activeItem = target.querySelector(".cluster-item.active");
  if (activeItem) {
    requestAnimationFrame(() => activeItem.scrollIntoView({
      block: "nearest",
      inline: "nearest",
      behavior: "auto",
    }));
  }
  const first = rendered.length ? state.clusterWindowStart + 1 : 0;
  const last = state.clusterWindowStart + rendered.length;
  $("#cluster-window-status").textContent =
    `${first}–${last} of ${state.visible.length}`;
  $("#previous-cluster-window").disabled = state.clusterWindowStart === 0;
  $("#next-cluster-window").disabled = last >= state.visible.length;
  $("#cluster-window-controls").hidden = state.visible.length <= state.clusterWindowSize;
}

function moveClusterWindow(direction) {
  state.clusterWindowStart = Math.max(
    0, state.clusterWindowStart + direction * state.clusterWindowSize);
  renderClusterList();
  $("#cluster-list").scrollTop = 0;
  $("#cluster-list .cluster-item")?.focus();
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
}

function renderReviewWorkspace(cluster, decision) {
  const draft = draftFor(cluster);
  const effectiveKeepers = draft.reviewed ? draft.keepers : decision.photos;
  const selection = $("#workspace-selection");
  selection.textContent =
    `${effectiveKeepers.length} of ${cluster.photos.length} selected`;
  selection.dataset.source = draft.reviewed ? "human" : "ai";

  const reviewState = $("#workspace-review-state");
  reviewState.textContent = !draft.reviewed
    ? "AI recommendation awaiting review"
    : arraysEqual(draft.keepers, decision.photos)
      ? "Reviewed · AI choice accepted"
      : "Reviewed · human selection modified";
  reviewState.className = draft.reviewed ? "reviewed" : "pending";

  const tray = $("#compare-tray");
  tray.hidden = state.compare.length === 0;
  const items = $("#compare-items");
  items.replaceChildren();
  state.compare.forEach((name, index) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "compare-chip";
    chip.setAttribute("aria-label", `Remove ${name} from comparison`);
    const number = document.createElement("span");
    number.textContent = String(index + 1);
    const label = document.createElement("strong");
    label.textContent = name;
    const remove = document.createElement("span");
    remove.textContent = "×";
    remove.setAttribute("aria-hidden", "true");
    chip.append(number, label, remove);
    chip.addEventListener("click", () => toggleCompare(name));
    items.append(chip);
  });
  $("#compare-guidance").textContent = state.compare.length < 2
    ? "Choose one more frame for side-by-side review"
    : "Ready for synchronized side-by-side review";
  $("#open-comparison").disabled = state.compare.length === 0;

  document.querySelectorAll(".density-button").forEach((button) => {
    const active = button.dataset.density === state.viewDensity;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $(".review").classList.toggle("density-compact", state.viewDensity === "compact");
}

function photoFormatLabel(name, rawFiles = []) {
  const suffix = name.includes(".") ? name.split(".").pop().toUpperCase() : "PHOTO";
  const rawSuffixes = ["RAF", "RAW", "DNG", "CR2", "CR3", "NEF", "ARW", "ORF", "RW2"];
  return rawSuffixes.includes(suffix) ? "RAW"
    : rawFiles.length ? `${suffix} + RAW` : suffix;
}

function savePhotoAnnotation(cluster, name, annotation) {
  const current = draftFor(cluster);
  current.photo_annotations[name] = {
    flag: annotation.flag,
    rating: Number(annotation.rating),
    label: annotation.label,
  };
  if (annotation.flag === "keep" && !current.keepers.includes(name)) {
    current.keepers.push(name);
  } else if (annotation.flag === "reject") {
    current.keepers = current.keepers.filter((item) => item !== name);
  }
  current.reviewed = true;
  saveCluster(cluster, current, `${name} judgment saved`, "photo-judgment");
  showCluster(cluster.cluster_id);
}

function photoCard(cluster, decision, name, position) {
  const template = $("#photo-card-template");
  const card = template.content.firstElementChild.cloneNode(true);
  const aiSelected = decision.photos.includes(name);
  const draft = draftFor(cluster);
  const humanSelected = draft.keepers.includes(name);
  const annotation = draft.photo_annotations[name] || {
    rating: 0, flag: "unrated", label: "",
  };
  if (aiSelected) card.classList.add("selected");
  if (humanSelected) card.classList.add("effective-selected");
  if (draft.reviewed && humanSelected) card.classList.add("human-selected");
  if (draft.reviewed && aiSelected && !humanSelected) card.classList.add("human-rejected");
  if (draft.reviewed && !aiSelected && humanSelected) card.classList.add("human-promoted");
  if (state.activeReviewPhoto === name) card.classList.add("inspector-focused");
  card.dataset.photoName = name;
  card.setAttribute(
    "aria-label",
    `Photo ${position + 1} of ${cluster.photos.length}: ${name}`,
  );
  const image = card.querySelector("img");
  image.alt = name;
  bindPreviewImage(image, name, "thumb");
  image.addEventListener("error", () => {
    image.hidden = true;
    const error = card.querySelector(".image-error");
    error.hidden = false;
    error.textContent = "Preview unavailable · Retry";
  });
  card.querySelector(".filename").textContent = name;
  card.querySelector(".filename").title = name;
  card.querySelector(".photo-number").textContent = String(position + 1);
  card.querySelector(".keeper-badge").textContent =
    aiSelected ? "AI keeper" : "AI did not select";
  const humanBadge = card.querySelector(".human-badge");
  humanBadge.textContent = draft.reviewed
    ? (humanSelected ? "Kept by human" : "Not kept by human")
    : "Awaiting human review";
  const rawFiles = state.rawSource?.matches?.[name] || [];
  const rawBadge = card.querySelector(".raw-match-badge");
  rawBadge.textContent = rawFiles.length === 1
    ? "RAW available"
    : rawFiles.length > 1 ? `${rawFiles.length} RAW matches` : "JPEG only";
  rawBadge.classList.toggle("available", rawFiles.length === 1);
  rawBadge.classList.toggle("ambiguous", rawFiles.length > 1);
  rawBadge.title = rawFiles.join(", ") || "No matching RAW in the linked folder";
  const formatBadge = card.querySelector(".photo-format-badge");
  formatBadge.textContent = photoFormatLabel(name, rawFiles);
  formatBadge.title = rawBadge.title;
  const imageButton = card.querySelector(".photo-image-surface");
  imageButton.setAttribute(
    "aria-label",
    `${humanSelected ? "Deselect" : "Select"} ${name}; double-click or press Space to preview`,
  );
  imageButton.title = "Click to select · Double-click to preview";
  let selectionTimer = null;
  imageButton.addEventListener("click", (event) => {
    if (event.target.closest(".image-error")) return;
    focusReviewPhoto(name, "photo");
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(() => toggleHumanKeeper(cluster, name), 220);
  });
  imageButton.addEventListener("dblclick", (event) => {
    if (event.target.closest(".image-error")) return;
    clearTimeout(selectionTimer);
    openViewer([name]);
  });
  imageButton.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      toggleHumanKeeper(cluster, name);
    } else if (event.key === " ") {
      event.preventDefault();
      openViewer([name]);
    }
  });
  imageButton.addEventListener("focus", () => focusReviewPhoto(name, "photo"));
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
    card.querySelector(".photo-details-metadata").append(line);
  }
  const toggle = card.querySelector(".human-toggle");
  toggle.querySelector(".selection-mark").textContent = humanSelected ? "✓" : "";
  toggle.classList.toggle("active", humanSelected);
  toggle.disabled = !state.review.status.compatible;
  toggle.setAttribute("aria-pressed", String(humanSelected));
  toggle.setAttribute(
    "aria-label",
    `${humanSelected ? "Remove" : "Keep"} ${name} (${position + 1})`,
  );
  if (position < 9) toggle.setAttribute("aria-keyshortcuts", String(position + 1));
  toggle.addEventListener("click", () => {
    focusReviewPhoto(name, "photo");
    toggleHumanKeeper(cluster, name);
  });
  const compare = card.querySelector(".compare-button");
  const comparisonSelected = state.compare.includes(name);
  if (comparisonSelected) compare.classList.add("active");
  compare.textContent = comparisonSelected ? "Comparing" : "Compare";
  compare.setAttribute("aria-pressed", String(comparisonSelected));
  compare.setAttribute("aria-label", `${comparisonSelected ? "Remove" : "Add"} ${name} ${
    comparisonSelected ? "from" : "to"} comparison`);
  compare.addEventListener("click", () => {
    focusReviewPhoto(name, "photo");
    toggleCompare(name);
  });
  const detailsButton = card.querySelector(".photo-details-button");
  const details = card.querySelector(".photo-info");
  detailsButton.setAttribute("aria-label", `Show details and annotations for ${name}`);
  detailsButton.addEventListener("click", () => {
    const expanded = details.hidden;
    details.hidden = !expanded;
    card.classList.toggle("details-open", expanded);
    detailsButton.setAttribute("aria-expanded", String(expanded));
    detailsButton.textContent = expanded ? "Hide info" : "Info";
  });
  const flag = card.querySelector(".photo-flag");
  const rating = card.querySelector(".photo-rating");
  const label = card.querySelector(".photo-label");
  flag.value = annotation.flag;
  rating.value = String(annotation.rating);
  label.value = annotation.label;
  const saveJudgment = () => savePhotoAnnotation(cluster, name, {
    flag: flag.value, rating: rating.value, label: label.value,
  });
  [flag, rating, label].forEach((control) => {
    control.disabled = !state.review.status.compatible;
    control.addEventListener("change", saveJudgment);
  });
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

function setInspectorTab(tab) {
  state.inspectorTab = ["group", "photo", "recovery"].includes(tab) ? tab : "group";
  document.querySelectorAll(".inspector-tab").forEach((button) => {
    const active = button.dataset.inspectorTab === state.inspectorTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  for (const name of ["group", "photo", "recovery"]) {
    $(`#inspector-${name}-panel`).hidden = name !== state.inspectorTab;
  }
  $("#inspector-context-title").textContent = {
    group: "Group", photo: "Photo", recovery: "Recovery",
  }[state.inspectorTab];
}

function setInspectorOpen(open) {
  state.inspectorOpen = Boolean(open);
  $("#review-workspace").classList.toggle("inspector-collapsed", !state.inspectorOpen);
  $("#review-inspector").hidden = !state.inspectorOpen;
  $("#inspector-toggle").setAttribute("aria-pressed", String(state.inspectorOpen));
  $("#inspector-toggle").classList.toggle("active", state.inspectorOpen);
}

function focusReviewPhoto(name, tab = state.inspectorTab) {
  const cluster = state.clusters.find((item) => item.cluster_id === state.activeClusterId);
  if (!cluster?.photos.includes(name)) return;
  state.activeReviewPhoto = name;
  document.querySelectorAll("#photo-grid .photo-card").forEach((card) =>
    card.classList.toggle("inspector-focused", card.dataset.photoName === name));
  setInspectorTab(tab);
  renderReviewInspector(cluster, decisionFor(cluster));
}

function appendInspectorMetadata(target, metrics) {
  target.replaceChildren();
  if (!metrics || typeof metrics !== "object") {
    const row = document.createElement("div");
    const term = document.createElement("dt"); term.textContent = "Status";
    const value = document.createElement("dd"); value.textContent = "No local measurements stored";
    row.append(term, value); target.append(row); return;
  }
  const preferred = [
    ["captured", "Captured"], ["technical_score", "Technical score"],
    ["sharpness", "Sharpness"], ["brightness", "Brightness"],
    ["width", "Width"], ["height", "Height"], ["bytes", "File size"],
  ];
  const seen = new Set();
  for (const [field, label] of preferred) {
    if (!(field in metrics) || metrics[field] == null || metrics[field] === "") continue;
    seen.add(field);
    const row = document.createElement("div");
    const term = document.createElement("dt"); term.textContent = label;
    const value = document.createElement("dd");
    value.textContent = field === "bytes" ? formatBytes(Number(metrics[field])) : String(metrics[field]);
    row.append(term, value); target.append(row);
  }
  for (const [field, raw] of Object.entries(metrics)) {
    if (seen.has(field) || raw == null || typeof raw === "object") continue;
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = field.replaceAll("_", " ").replace(/^./, (value) => value.toUpperCase());
    const value = document.createElement("dd"); value.textContent = String(raw);
    row.append(term, value); target.append(row);
  }
}

function renderInspectorAssessments(decision) {
  const target = $("#inspector-assessments");
  target.replaceChildren();
  const assessments = Array.isArray(decision.photographic_assessment)
    ? decision.photographic_assessment : [];
  if (!assessments.length) {
    const message = document.createElement("p");
    message.className = "inspector-muted";
    message.textContent = "No frame-by-frame assessment was stored.";
    target.append(message); return;
  }
  for (const assessment of assessments) {
    const article = document.createElement("article");
    article.className = "inspector-assessment";
    const title = document.createElement("button");
    title.type = "button";
    title.textContent = assessment.filename || "Unknown photo";
    title.addEventListener("click", () => focusReviewPhoto(assessment.filename, "photo"));
    const copy = document.createElement("p");
    copy.textContent = [
      assessment.pose_and_body, assessment.eyes_and_gaze,
      assessment.mouth_and_expression, assessment.readiness_and_timing,
    ].filter(Boolean).join(" ") || "No concise assessment available.";
    article.append(title, copy); target.append(article);
  }
}

function renderReviewInspector(cluster, decision) {
  if (!cluster || !decision) return;
  const draft = draftFor(cluster);
  $("#inspector-group-title").textContent = cluster.cluster_id;
  $("#inspector-group-state").textContent = draft.reviewed ? "Reviewed" : "Unreviewed";
  $("#inspector-group-state").className = draft.reviewed ? "reviewed" : "";
  $("#inspector-group-summary").textContent =
    `${cluster.photos.length} photograph${cluster.photos.length === 1 ? "" : "s"} · ` +
    `${decision.photos.length} recommended keeper${decision.photos.length === 1 ? "" : "s"}`;
  const confidence = Math.max(0, Math.min(1, Number(decision.confidence || 0)));
  $("#inspector-confidence-value").textContent = `${Math.round(confidence * 100)}%`;
  $("#inspector-confidence-bar").style.width = `${confidence * 100}%`;
  $("#inspector-rationale").textContent = decision.rationale || "No rationale stored.";
  const keepers = $("#inspector-keepers"); keepers.replaceChildren();
  for (const name of decision.photos) {
    const chip = document.createElement("button"); chip.type = "button";
    chip.textContent = name; chip.addEventListener("click", () => focusReviewPhoto(name, "photo"));
    keepers.append(chip);
  }
  if (!decision.photos.length) {
    const empty = document.createElement("span"); empty.textContent = "No AI keeper"; keepers.append(empty);
  }
  renderInspectorAssessments(decision);

  const name = cluster.photos.includes(state.activeReviewPhoto)
    ? state.activeReviewPhoto : decision.photos[0] || cluster.photos[0] || null;
  state.activeReviewPhoto = name;
  const photoEmpty = $("#inspector-photo-empty");
  const photoContent = $("#inspector-photo-content");
  photoEmpty.hidden = Boolean(name); photoContent.hidden = !name;
  if (name) {
    const humanSelected = draft.keepers.includes(name);
    const aiSelected = decision.photos.includes(name);
    const rawFiles = state.rawSource?.matches?.[name] || [];
    const annotation = draft.photo_annotations[name] || {rating: 0, flag: "unrated", label: ""};
    const image = $("#inspector-photo-image");
    image.alt = name;
    bindPreviewImage(image, name, "thumb");
    $("#inspector-photo-title").textContent = name;
    $("#inspector-photo-format").textContent = photoFormatLabel(name, rawFiles);
    $("#inspector-photo-status").textContent = [
      humanSelected ? "Selected to keep" : "Not selected",
      aiSelected ? "AI recommended" : "AI did not recommend",
    ].join(" · ");
    $("#inspector-toggle-keeper").textContent = humanSelected ? "Remove from selection" : "Keep photograph";
    $("#inspector-toggle-keeper").disabled = !state.review.status.compatible;
    const comparisonSelected = state.compare.includes(name);
    $("#inspector-compare-photo").textContent = comparisonSelected ? "Remove comparison" : "Compare";
    $("#inspector-compare-photo").classList.toggle("active", comparisonSelected);
    $("#inspector-raw-status").textContent = rawFiles.length
      ? rawFiles.join("\n") : "No matching RAW file is linked for this photograph.";
    appendInspectorMetadata($("#inspector-photo-metadata"), state.payload.measurements[name]);
    $("#inspector-photo-flag").value = annotation.flag;
    $("#inspector-photo-rating").value = String(annotation.rating);
    $("#inspector-photo-label").value = annotation.label;
    for (const control of ["#inspector-photo-flag", "#inspector-photo-rating", "#inspector-photo-label"]) {
      $(control).disabled = !state.review.status.compatible;
    }
  }

  const currentMissing = cluster.photos.filter(
    (photo) => state.payload.missing_photos.includes(photo));
  const totalMissing = Number(state.payload.summary?.missing_photos || 0);
  $("#inspector-recovery-title").textContent = totalMissing
    ? `${totalMissing} source file${totalMissing === 1 ? "" : "s"} unavailable` : "Sources available";
  $("#inspector-recovery-summary").textContent = totalMissing
    ? "Reconnect the original volume or open recovery guidance. Cached previews remain safe to review."
    : "The report can currently reach its referenced source photographs.";
  const missingTarget = $("#inspector-missing-files"); missingTarget.replaceChildren();
  for (const missing of currentMissing) {
    const item = document.createElement("span"); item.textContent = missing; missingTarget.append(item);
  }
  if (!currentMissing.length) {
    const item = document.createElement("span"); item.textContent = "No missing files in this group";
    item.className = "available"; missingTarget.append(item);
  }
  $("#inspector-raw-root").textContent = state.rawSource?.root || "No RAW folder linked.";
}

function showCluster(clusterId) {
  const cluster = state.clusters.find((item) => item.cluster_id === clusterId);
  if (!cluster) return;
  const decision = decisionFor(cluster);
  const clusterChanged = state.activeClusterId !== clusterId;
  if (clusterChanged) {
    state.compare = [];
    state.activeReviewPhoto = decision.photos[0] || cluster.photos[0] || null;
    state.inspectorTab = "group";
  } else if (!cluster.photos.includes(state.activeReviewPhoto)) {
    state.activeReviewPhoto = decision.photos[0] || cluster.photos[0] || null;
  }
  state.activeClusterId = clusterId;
  $("#review-activity").textContent = "Reviewing";
  $("#review-activity").className = "active";
  $("#empty-state").hidden = true;
  $("#cluster-view").hidden = false;
  const absolutePosition = state.clusters.indexOf(cluster) + 1;
  $("#cluster-position").textContent = `CLUSTER ${absolutePosition} OF ${state.clusters.length}`;
  $("#cluster-title").textContent = cluster.cluster_id;
  $("#cluster-subtitle").textContent =
    `${cluster.photos.length} photographs · ${decision.photos.length} recommended keeper${decision.photos.length === 1 ? "" : "s"}`;
  const reviewedCount = Number(state.review.status.reviewed_clusters || 0);
  const remaining = Math.max(0, state.clusters.length - reviewedCount);
  $("#review-remaining").textContent =
    `${reviewedCount} reviewed · ${remaining} remaining`;
  $("#cluster-progress-bar").style.width =
    `${Math.max(0, Math.min(100, absolutePosition / state.clusters.length * 100))}%`;
  $("#confidence").textContent = `${Math.round(Number(decision.confidence || 0) * 100)}% curator confidence`;
  $("#rationale").textContent = decision.rationale || "No rationale stored.";
  $("#raw-decision").textContent = JSON.stringify(decision, null, 2);
  renderAlerts(cluster, decision);
  const grid = $("#photo-grid");
  grid.replaceChildren(...cluster.photos.map(
    (name, position) => photoCard(cluster, decision, name, position)));
  renderReviewWorkspace(cluster, decision);
  renderHumanReview(cluster, decision);
  renderAssessments(decision);
  setInspectorTab(state.inspectorTab);
  renderReviewInspector(cluster, decision);
  renderClusterList();
  updateNavigation();
  $(".review").scrollTo({top: 0, behavior: "auto"});
  persistPosition(clusterId);
  const adjacent = [];
  const visiblePosition = state.visible.findIndex(
    (item) => item.cluster_id === clusterId);
  for (const offset of [1, -1, 2]) {
    const neighbor = state.visible[visiblePosition + offset];
    if (neighbor) adjacent.push(...neighbor.photos);
  }
  focusPreviews(cluster.photos, adjacent, "thumb");
}

function updateNavigation() {
  const index = state.visible.findIndex((item) => item.cluster_id === state.activeClusterId);
  $("#previous-cluster").disabled = index <= 0;
  $("#next-cluster").disabled = index < 0 || index >= state.visible.length - 1;
  $("#skip-group").disabled = index < 0 || index >= state.visible.length - 1;
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
}

function setSaveStatus(message, kind = "") {
  const target = $("#save-status");
  target.textContent = message;
  target.className = `save-status ${kind}`.trim();
  if (kind === "error" || (!kind && !/^(No |Loading|Saving)/.test(message))) {
    showToast(message, kind === "error" ? "error" : "success");
  }
}

function showToast(message, tone = "success") {
  const region = $("#toast-region");
  if (!region) return;
  clearTimeout(state.toastTimer);
  region.textContent = "";
  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;
  const mark = document.createElement("span");
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = tone === "error" ? "!" : "✓";
  const text = document.createElement("span");
  text.textContent = message;
  toast.append(mark, text);
  region.append(toast);
  requestAnimationFrame(() => toast.classList.add("visible"));
  state.toastTimer = setTimeout(() => {
    toast.classList.remove("visible");
    setTimeout(() => toast.remove(), 180);
  }, 3200);
}

function toggleShortcuts() {
  const dialog = $("#shortcuts-dialog");
  if (dialog.open) dialog.close();
  else dialog.showModal();
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
        photo_annotations: structuredClone(stored.photo_annotations || {}),
      });
    }
    renderHumanReview(active, decisionFor(active));
    const grid = $("#photo-grid");
    grid.replaceChildren(...active.photos.map(
      (name, position) => photoCard(active, decisionFor(active), name, position)));
    renderReviewWorkspace(active, decisionFor(active));
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
    return true;
  }).catch((error) => {
    setSaveStatus(error.message, "error");
    return false;
  });
  return state.saveQueue;
}

function saveCluster(cluster, draft, message = "Human review saved", action = "review") {
  state.drafts.set(cluster.cluster_id, {
    keepers: [...draft.keepers],
    note: draft.note,
    reviewed: draft.reviewed,
    photo_annotations: structuredClone(draft.photo_annotations || {}),
  });
  renderClusterList();
  const saved = enqueueSave("/api/review/cluster", () => {
    const current = state.drafts.get(cluster.cluster_id);
    return {
      cluster_id: cluster.cluster_id,
      keepers: current.keepers,
      note: current.note,
      reviewed: current.reviewed,
      photo_annotations: current.photo_annotations,
      action,
    };
  }, message);
  saved.then((success) => {
    if (success) return;
    const stored = humanReview(cluster.cluster_id);
    state.drafts.set(cluster.cluster_id, {
      keepers: stored ? [...stored.keepers] : [...decisionFor(cluster).photos],
      note: stored ? stored.note : "",
      reviewed: stored ? stored.reviewed : false,
      photo_annotations: structuredClone(stored?.photo_annotations || {}),
    });
    renderClusterList();
  });
  return saved;
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
    label.textContent = "Review is read-only";
    label.classList.add("modified");
  } else if (!draft.reviewed) {
    label.textContent = "Unreviewed";
  } else if (modified) {
    label.textContent = "Human decision differs";
    label.classList.add("modified");
  } else {
    label.textContent = "Human agrees with AI";
    label.classList.add("reviewed");
  }
  const note = $("#review-note");
  if (note.value !== draft.note) note.value = draft.note;
  const disabled = !state.review.status.compatible;
  note.disabled = disabled;
  ["#accept-ai-next", "#accept-ai", "#keep-none", "#mark-reviewed", "#reset-review"].forEach(
    (selector) => { $(selector).disabled = disabled; });
  $("#actionbar-undo").disabled = disabled || !state.review.history.length;
}

function updateActiveReview(transform, message) {
  const cluster = state.clusters.find(
    (item) => item.cluster_id === state.activeClusterId);
  if (!cluster) return Promise.resolve(false);
  const updated = transform({...draftFor(cluster), keepers: [...draftFor(cluster).keepers]});
  const saved = saveCluster(cluster, updated, message);
  showCluster(cluster.cluster_id);
  return saved;
}

function acceptAIRecommendation(message = "AI recommendation accepted") {
  return updateActiveReview((draft) => {
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    return {...draft, keepers: [...decisionFor(cluster).photos], reviewed: true};
  }, message);
}

async function acceptSelectionAndNext() {
  const clusterId = state.activeClusterId;
  const saved = await updateActiveReview(
    (draft) => ({...draft, reviewed: true}), "Selection accepted");
  if (saved && state.activeClusterId === clusterId) navigate(1);
}

function skipActiveGroup() {
  const current = state.activeClusterId;
  navigate(1);
  if (state.activeClusterId !== current) setSaveStatus("Group skipped without changing its review state");
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

async function downloadXmp() {
  const policy = $("#export-policy").value;
  try {
    const response = await fetch(`/api/export/xmp?policy=${encodeURIComponent(policy)}`);
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `XMP export failed (${response.status})`);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "opencull-lightroom-xmp.zip";
    link.click();
    URL.revokeObjectURL(link.href);
    setSaveStatus("Lightroom XMP sidecars exported");
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

const selectionPolicyPresentation = {
  human_only: {
    title: "Reviewed human decisions only",
    description: "Includes keepers only from clusters you explicitly reviewed. Unreviewed clusters contribute no photographs.",
    risk: "Safest when delivery must reflect human judgment only.",
  },
  effective: {
    title: "Human decisions with AI fallback",
    description: "Uses your decision for reviewed clusters and the AI recommendation everywhere else.",
    risk: "May include photographs that have not received human review.",
  },
  require_all: {
    title: "Complete human review required",
    description: "Uses the effective decision, but refuses export until every cluster has been reviewed.",
    risk: "Best final-delivery policy when completeness matters.",
  },
  ai_only: {
    title: "Original AI recommendations only",
    description: "Ignores later human changes and exports the curator’s original selections.",
    risk: "Human corrections will not be included.",
  },
  modified_only: {
    title: "Human changes only",
    description: "Includes keepers only where your reviewed decision differs from the AI recommendation.",
    risk: "Useful for auditing corrections, not as a complete delivery.",
  },
};

async function loadCleanupCandidates(force = false) {
  if (state.cleanupCandidates && !force) {
    renderCleanupCandidates(state.cleanupCandidates);
    return;
  }
  try {
    const response = await fetch("/api/action/cleanup-candidates");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not inspect quarantine");
    state.cleanupCandidates = payload;
    renderCleanupCandidates(payload);
  } catch (error) {
    $("#cleanup-inventory-summary").textContent = error.message;
    $("#cleanup-candidate-list").replaceChildren(alert(error.message, "fallback"));
  }
}

function renderCleanupCandidates(payload) {
  const summary = payload.summary || {};
  $("#cleanup-inventory-summary").textContent =
    `${summary.files || 0} files · ${summary.raw_files || 0} RAW · ` +
    `${summary.jpeg_files || 0} JPEG · ${formatBytes(summary.bytes || 0)}`;
  const list = $("#cleanup-candidate-list");
  list.replaceChildren();
  for (const candidate of payload.candidates || []) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = candidate.relative_path;
    input.addEventListener("change", () => {
      state.operationPlan = null;
      $("#preflight-result").hidden = true;
    });
    const name = document.createElement("span");
    name.textContent = candidate.relative_path;
    const detail = document.createElement("small");
    detail.textContent = `${candidate.kind} · ${formatBytes(candidate.bytes)}`;
    label.append(input, name, detail);
    list.append(label);
  }
  if (!(payload.candidates || []).length) {
    list.append(alert("The project Rejected folder is empty.", "info"));
  }
}

function renderSelectionPolicy() {
  const policy = selectionPolicyPresentation[$("#export-policy").value];
  $("#policy-title").textContent = policy.title;
  $("#policy-description").textContent = policy.description;
  $("#policy-risk").textContent = policy.risk;
  state.operationPlan = null;
  $("#preflight-result").hidden = true;
}

function renderOperationRisk() {
  const action = $("#operation-action").value;
  const projectFilter = action === "project_filter";
  const systemCleanup = action === "system_cleanup";
  const move = action === "move";
  const trash = action === "trash";
  const badge = $("#operation-risk-badge");
  badge.textContent = systemCleanup
    ? "Final System Trash boundary"
    : projectFilter
    ? "Recoverable project filter"
    : trash
    ? "Unselected files go to Trash"
    : move ? "Source files will move" : "Verified copy";
  badge.className = `operation-risk-badge ${systemCleanup || move || trash ? "move" : "copy"}`;
  $("#operation-risk-message").textContent = systemCleanup
    ? "Final cleanup operates only on the project's recoverable Rejected folder. Useful RAW Reserve files stay untouched by default. Every authorized file is hash-verified into the correct macOS Trash and remains rollbackable while the Trash copy exists."
    : projectFilter
    ? "After every cluster has a human review, rejected photographs move into this project's Rejected folder. Matching RAWs for keepers move into RAW Reserve; external RAWs are copied and their source archive remains untouched. The complete operation is reversible."
    : trash
    ? "Only unselected photographs are moved to the macOS Trash after verification. Darkimiya retains a journal so the operation can be resumed or rolled back."
    : move
      ? "Move removes each source only after its destination copy passes SHA-256 verification. A rollback journal is retained."
      : "Copy creates SHA-256 verified duplicates and leaves every source photograph in place.";
  const scope = $("#operation-scope");
  const layout = $("#operation-layout");
  const destination = $("#operation-destination");
  const picker = $("#pick-operation-destination");
  if (trash) scope.value = "unselected";
  if (projectFilter) scope.value = "unselected";
  scope.disabled = trash || projectFilter || systemCleanup;
  layout.disabled = projectFilter || systemCleanup;
  destination.disabled = trash || projectFilter || systemCleanup;
  picker.disabled = trash || projectFilter || systemCleanup;
  $("#preserve-relative").disabled = projectFilter || systemCleanup;
  $("#include-companions").disabled = projectFilter || systemCleanup;
  $("#cleanup-policy-field").hidden = !systemCleanup;
  const inspect = systemCleanup && $("#cleanup-policy").value === "inspect";
  $("#cleanup-inspector").hidden = !inspect;
  if (systemCleanup) loadCleanupCandidates();
  state.operationPlan = null;
  $("#preflight-result").hidden = true;
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
  const projectFilter = plan.action === "project_filter";
  const systemCleanup = plan.action === "system_cleanup";
  $("#plan-summary").replaceChildren(
    planStat("Files", plan.summary.files),
    planStat(systemCleanup ? "To System Trash" : projectFilter ? "RAW reserve" : "Selected",
      systemCleanup ? plan.summary.trashed_files : plan.summary.selected_files),
    planStat(systemCleanup ? "RAWs retained" : projectFilter ? "Rejected" : "Unselected",
      systemCleanup ? plan.summary.retained_raw_files : plan.summary.unselected_files),
    planStat(systemCleanup ? "Quarantine total" : projectFilter ? "External RAW copies" : "Companions",
      systemCleanup ? plan.summary.quarantine_files
        : projectFilter ? plan.summary.external_raw_copies : plan.summary.companion_files),
    planStat("Required", formatBytes(plan.summary.bytes)),
    planStat("Free", formatBytes(plan.summary.free_bytes)),
  );
  const unreviewed = Number(plan.summary.unreviewed_clusters || 0);
  const reviewWarning = $("#plan-review-warning");
  reviewWarning.textContent = unreviewed
    ? `${unreviewed} unreviewed ${unreviewed === 1 ? "cluster uses" : "clusters use"} the selected policy’s fallback behavior.`
    : "Every cluster contributing to this plan has a human review.";
  reviewWarning.className =
    `plan-review-warning ${unreviewed ? "attention" : "complete"}`;
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
  const expected = plan.confirmation_code;
  $("#confirmation-help").textContent =
    `Type the four-digit code “${expected}” to authorize this verified ${
      plan.action}.`;
  $("#operation-confirmation").value = "";
  $("#execute-operation").disabled = true;
  $("#execute-operation").dataset.expected = expected;
  $("#operation-confirmation-block").hidden = Boolean(errors.length);
  $("#preflight-result").scrollIntoView({behavior: "smooth", block: "start"});
}

async function runPreflight() {
  try {
    const destination = $("#operation-destination").value.trim();
    const action = $("#operation-action").value;
    if (!["trash", "project_filter", "system_cleanup"].includes(action) && !destination) {
      throw new Error("Choose a destination folder before verifying the plan.");
    }
    $("#run-preflight").disabled = true;
    $("#run-preflight").textContent = "Checking every file…";
    const plan = await operationalPost("/api/action/preflight", {
      destination,
      policy: $("#export-policy").value,
      action,
      selection_scope: $("#operation-scope").value,
      layout: $("#operation-layout").value,
      preserve_relative: $("#preserve-relative").checked,
      include_companions: $("#include-companions").checked,
      cleanup_policy: $("#cleanup-policy").value,
      selected_cleanup_paths: Array.from(
        document.querySelectorAll("#cleanup-candidate-list input:checked"),
        (input) => input.value),
    });
    renderPlan(plan);
  } catch (error) {
    setSaveStatus(error.message, "error");
  } finally {
    $("#run-preflight").disabled = false;
    $("#run-preflight").textContent = "Verify exact plan";
  }
}

async function pickOperationDestination() {
  try {
    const result = await operationalPost(
      "/api/action/pick-destination", {});
    $("#operation-destination").value = result.destination;
    state.operationPlan = null;
    $("#preflight-result").hidden = true;
    setSaveStatus("Destination folder selected");
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
  const percent = total ? Math.round(completed / total * 100) : 0;
  $("#operation-progress-percent").textContent = `${percent}%`;
  $("#operation-progress-text").textContent =
    `${operation.status} · ${completed} of ${total} files · ` +
    `${formatBytes(operation.completed_bytes || 0)} verified`;
  const presentation = {
    planned: ["Ready to begin", productStatusLanguage.ready],
    running: ["Verifying file operation", productStatusLanguage.working],
    paused: ["Operation paused safely", productStatusLanguage.paused],
    completed: ["Operation completed and verified", productStatusLanguage.verified],
    failed: ["Operation needs attention", productStatusLanguage.attention],
    "rolled-back": ["Move rolled back", "Rolled back"],
  }[operation.status] || ["Operation journal", operation.status];
  $("#operation-state-title").textContent = presentation[0];
  $("#operation-state-badge").textContent = presentation[1];
  $("#operation-state-badge").className =
    `operation-state-badge ${operation.status}`;
  const complete = operation.status === "completed";
  $("#operation-receipt").hidden = !complete;
  if (complete) {
    $("#receipt-operation").textContent =
      `${String(operation.action).replace("_", " ")} · ${operation.plan_id}`;
    $("#receipt-files").textContent = `${completed} of ${total}`;
    $("#receipt-bytes").textContent =
      formatBytes(operation.completed_bytes || 0);
    $("#receipt-destination").textContent = operation.destination || "—";
    $("#receipt-journal").textContent = operation.journal_path || "Saved beside report";
    if (operation.action === "system_cleanup") loadCleanupCandidates(true);
  }
  if (operation.status === "rolled-back" && operation.action === "system_cleanup") {
    loadCleanupCandidates(true);
  }
  if (operation.error) {
    $("#operation-progress-text").textContent += ` · ${operation.error}`;
  }
  const running = ["planned", "running", "paused"].includes(operation.status);
  $("#cancel-operation").hidden = !running;
  $("#rollback-operation").hidden =
    !["move", "trash", "project_filter", "system_cleanup"].includes(operation.action) ||
    !["completed", "failed", "paused"].includes(operation.status);
  $("#operation-progress").scrollIntoView({behavior: "smooth", block: "start"});
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
    setSaveStatus(
      `${error.message} · progress refresh will retry`, "error");
    if (state.operationId === operationId) {
      state.operationPoll = setTimeout(
        () => monitorOperation(operationId), 1500);
    }
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

async function resumeDetectedOperation(journal) {
  const confirmation = `RESUME ${journal.plan_id}`;
  if (!window.confirm(
    `Resume the verified ${journal.action} operation?\n\n` +
    `${journal.remaining_files} files remain.\n` +
    `Destination: ${journal.destination}`)) return;
  try {
    const operation = await operationalPost("/api/action/resume", {
      journal_path: journal.journal_path,
      confirmation,
    });
    state.operationId = operation.plan_id;
    renderOperation(operation);
    monitorOperation(operation.plan_id);
    renderRecoverableOperations([]);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function renderRecoverableOperations(journals) {
  state.recoverableOperations = journals;
  const target = $("#recoverable-operations");
  target.replaceChildren();
  $("#recovery-operation-status").textContent = journals.length
    ? `${journals.length} interrupted operation${
      journals.length === 1 ? "" : "s"} found for this report.`
    : "No interrupted operation was found for this report.";
  for (const journal of journals) {
    const card = document.createElement("article");
    card.className = "recoverable-operation";
    const summary = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent =
      `${String(journal.action).toUpperCase()} · ${journal.status}`;
    const progress = document.createElement("span");
    progress.textContent =
      `${journal.completed_files} of ${journal.total_files} verified · ` +
      `${journal.remaining_files} remaining`;
    const destination = document.createElement("code");
    destination.textContent = journal.destination;
    summary.append(title, progress, destination);
    const resume = document.createElement("button");
    resume.type = "button";
    resume.className = "primary-button";
    resume.textContent = "Resume remaining files";
    resume.addEventListener(
      "click", () => resumeDetectedOperation(journal));
    card.append(summary, resume);
    target.append(card);
  }
}

async function refreshRecoverableOperations(showBanner = false) {
  try {
    const response = await fetch("/api/action/recovery");
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Could not inspect operation journals");
    }
    const journals = payload.journals || [];
    renderRecoverableOperations(journals);
    if (showBanner && journals.length) {
      setRecoveryBanner(
        "Interrupted file operation found",
        `${journals[0].remaining_files} verified file operations can resume ` +
        "from the Delivery window.",
        "attention");
    }
  } catch (error) {
    $("#recovery-operation-status").textContent = error.message;
  }
}

function openViewer(names, filmstripNames = null) {
  const viewer = $("#viewer");
  $("#viewer-title").textContent = names.length > 1
    ? `${names.length} frames · ${names.join(" ↔ ")}`
    : names[0];
  const target = $("#viewer-images");
  target.style.setProperty("--viewer-columns", String(names.length));
  target.replaceChildren();
  const cluster = state.clusters.find(
    (item) => item.cluster_id === state.activeClusterId);
  const filmstrip = $("#viewer-filmstrip");
  filmstrip.replaceChildren();
  for (const candidate of filmstripNames || cluster?.photos || names) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "filmstrip-frame";
    if (names.includes(candidate)) button.classList.add("active");
    const thumb = document.createElement("img");
    thumb.alt = candidate;
    bindPreviewImage(thumb, candidate, "thumb");
    const label = document.createElement("span");
    label.textContent = candidate;
    button.append(thumb, label);
    button.addEventListener(
      "click", () => openViewer([candidate], filmstripNames));
    filmstrip.append(button);
  }
  resetViewerTransform();
  for (const name of names) {
    const pane = document.createElement("div");
    pane.className = "viewer-pane";
    const image = document.createElement("img");
    image.alt = name;
    bindPreviewImage(image, name, "detail");
    image.draggable = false;
    let start = null;
    image.addEventListener("pointerdown", (event) => {
      start = {x: event.clientX, y: event.clientY};
      image.setPointerCapture(event.pointerId);
    });
    image.addEventListener("pointermove", (event) => {
      if (!start) return;
      const dx = event.clientX - start.x;
      const dy = event.clientY - start.y;
      start = {x: event.clientX, y: event.clientY};
      if ($("#viewer-sync").checked) {
        state.viewer.x += dx;
        state.viewer.y += dy;
        applyViewerTransform();
      } else {
        const x = Number(image.dataset.panX || 0) + dx;
        const y = Number(image.dataset.panY || 0) + dy;
        image.dataset.panX = String(x);
        image.dataset.panY = String(y);
        applyImageTransform(image, x, y);
      }
    });
    image.addEventListener("pointerup", () => { start = null; });
    image.addEventListener("pointercancel", () => { start = null; });
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
  if (!viewer.open) viewer.showModal();
  applyViewerTransform();
  focusPreviews(names, [], "detail");
}

function applyImageTransform(image, x = state.viewer.x, y = state.viewer.y) {
  const cropZoom = state.viewer.crop === "face" ? 2.1 :
    state.viewer.crop === "detail" ? 2.8 : 1;
  const yBias = state.viewer.crop === "face" ? 18 : 0;
  image.style.transform =
    `translate(${x}px, ${y + yBias}px) scale(${state.viewer.zoom * cropZoom})`;
}

function applyViewerTransform() {
  document.querySelectorAll("#viewer-images img").forEach((image) => {
    applyImageTransform(
      image,
      $("#viewer-sync").checked ? state.viewer.x : Number(image.dataset.panX || 0),
      $("#viewer-sync").checked ? state.viewer.y : Number(image.dataset.panY || 0),
    );
  });
}

function resetViewerTransform() {
  state.viewer.zoom = 1;
  state.viewer.x = 0;
  state.viewer.y = 0;
  state.viewer.crop = "fit";
  if ($("#viewer-zoom")) $("#viewer-zoom").value = "1";
  if ($("#viewer-crop")) $("#viewer-crop").value = "fit";
  document.querySelectorAll("#viewer-images img").forEach((image) => {
    image.dataset.panX = "0";
    image.dataset.panY = "0";
  });
  applyViewerTransform();
}

function compareMarked() {
  if (!state.compare.length) {
    setSaveStatus("Mark one or more photographs with Compare first", "error");
    return;
  }
  openViewer(state.compare);
}

function undoReview() {
  if (!state.review?.history?.length) return;
  enqueueSave("/api/review/undo", () => ({}), "Last review change undone")
    .then(() => {
      state.drafts.clear();
      if (state.review.last_cluster_id) showCluster(state.review.last_cluster_id);
    });
}

function renderDetails() {
  const payload = state.payload;
  const report = payload.report;
  const fields = [
    ["Report", payload.report_path],
    ["Photos", payload.photos_root],
    ["Linked RAW folder", state.rawSource?.root || "Not configured"],
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

function renderRawSource() {
  const source = state.rawSource;
  const button = $("#raw-source-button");
  if (!source?.configured) {
    button.textContent = "Link RAW folder";
    button.title = "Choose a separate folder containing RAW originals";
    return;
  }
  const summary = source.summary;
  button.textContent = `RAW ${summary.matched}/${summary.photos}`;
  button.title = `${source.root} · ${summary.matched} matched · ${
    summary.ambiguous} ambiguous`;
}

async function pickRawSource() {
  const button = $("#raw-source-button");
  button.disabled = true;
  setSaveStatus("Waiting for RAW folder selection…");
  try {
    let path = "";
    const native = window.webkit?.messageHandlers?.openCullNative;
    if (native) {
      path = await new Promise((resolve) => {
        window.openCullRawFolderSelected = resolve;
        native.postMessage({action: "chooseRawFolder"});
      });
      window.openCullRawFolderSelected = null;
      if (!path) throw new Error("folder selection was cancelled");
      state.rawSource = await operationalPost(
        "/api/raw-source/configure", {path});
    } else {
      state.rawSource = await operationalPost("/api/raw-source/pick", {});
    }
    state.payload.raw_source = state.rawSource;
    renderRawSource();
    renderDetails();
    if (state.activeClusterId) showCluster(state.activeClusterId);
    if (state.shortlist) renderShortlistWorkspace();
    const summary = state.rawSource.summary;
    setSaveStatus(
      `${summary.matched} JPEGs matched to RAW${
        summary.ambiguous ? ` · ${summary.ambiguous} ambiguous` : ""}`);
  } catch (error) {
    if (!error.message.includes("cancelled")) {
      setSaveStatus(error.message, "error");
    }
  } finally {
    button.disabled = false;
  }
}

function recoveryCheck(label, detail, stateName = "passed") {
  const row = document.createElement("div");
  row.className = `recovery-check ${stateName}`;
  const icon = document.createElement("span");
  icon.textContent = stateName === "passed" ? "✓" :
    stateName === "attention" ? "!" : "×";
  const copy = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = label;
  const message = document.createElement("p");
  message.textContent = detail;
  copy.append(title, message);
  row.append(icon, copy);
  return row;
}

function setRecoveryBanner(title, message, severity = "attention") {
  const banner = $("#recovery-banner");
  state.recoveryIssues.set(title, {title, message, severity});
  const priority = {failed: 2, attention: 1};
  const issues = [...state.recoveryIssues.values()].sort(
    (left, right) => (priority[right.severity] || 0) - (priority[left.severity] || 0));
  const primary = issues[0];
  $("#recovery-banner-title").textContent = primary.title;
  $("#recovery-banner-message").textContent = primary.message +
    (issues.length > 1 ? ` · ${issues.length - 1} additional issue${issues.length === 2 ? "" : "s"}` : "");
  $("#open-recovery-from-banner").textContent = "Open recovery";
  banner.className = `recovery-banner ${primary.severity}`;
  banner.dataset.issueCount = String(issues.length);
  banner.hidden = false;
}

function renderRecoveryCenter(loadError = "") {
  const checks = $("#recovery-checks");
  checks.replaceChildren();
  const payload = state.payload;
  let attention = false;
  if (loadError) {
    attention = true;
    checks.append(recoveryCheck(
      "Report could not be loaded", loadError, "failed"));
    checks.append(recoveryCheck(
      "Original photographs", "No file operation was attempted.", "passed"));
  } else if (payload) {
    const missing = Number(payload.summary?.missing_photos || 0);
    const compatible = state.review?.status?.compatible !== false;
    checks.append(recoveryCheck(
      "Report structure", `${payload.summary.clusters} clusters loaded and validated.`));
    checks.append(recoveryCheck(
      "Photo volume",
      missing ? `${missing} source photographs are unavailable. Reconnect the original volume.`
        : `${payload.summary.photos} source photographs are available.`,
      missing ? "attention" : "passed"));
    const unreadable = Number(payload.summary?.unreadable_photos || 0);
    if (unreadable) {
      attention = true;
    }
    checks.append(recoveryCheck(
      "Scanner coverage",
      unreadable
        ? `${unreadable} photograph${unreadable === 1 ? "" : "s"} could not be decoded and ${unreadable === 1 ? "was" : "were"} not culled. They appear in the report under scan_errors.`
        : "Every photograph in the source folder was decoded and culled.",
      unreadable ? "attention" : "passed"));
    checks.append(recoveryCheck(
      "Human review binding",
      compatible ? "The review sidecar matches this report."
        : state.review.status.stale_reason,
      compatible ? "passed" : "failed"));
    checks.append(recoveryCheck(
      "Original-file protection",
      "Review and preview generation do not modify source photographs."));
    attention = Boolean(missing || !compatible);
  } else {
    checks.append(recoveryCheck(
      "Waiting for report", "Open an existing result or start culling from Queue.", "attention"));
    attention = true;
  }
  $("#recovery-health-icon").textContent = attention ? "!" : "✓";
  $("#recovery-health-icon").className =
    `recovery-health-icon ${attention ? "attention" : "healthy"}`;
  $("#recovery-health-title").textContent =
    attention ? "Darkimiya needs your attention" : "Review is healthy";
  $("#recovery-health-message").textContent = attention
    ? "Use the checks below to recover without losing review work."
    : "The report, source volume, and human review binding are available.";
}

async function loadProjectMigration() {
  const panel = $("#migration-panel");
  try {
    const response = await fetch("/api/project/migration");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Migration preview is unavailable");
    state.projectMigration = payload;
    panel.hidden = !payload.available;
    if (!payload.available) return;
    $("#migration-summary").textContent =
      `${payload.artifact_categories} evidence categories and ${payload.artifact_paths} linked files will be rebound. ` +
      `${payload.missing_artifact_paths.length} linked files are currently unavailable.`;
    $("#migration-code-hint").textContent = payload.confirmation_code;
    $("#migration-details").textContent = JSON.stringify({
      legacy_manifest: payload.legacy_path,
      legacy_sha256: payload.legacy_sha256,
      destination: payload.destination_path,
      missing_artifact_paths: payload.missing_artifact_paths,
      guarantees: {
        original_manifest_preserved: payload.original_manifest_preserved,
        photographs_moved: payload.photographs_moved,
      },
    }, null, 2);
  } catch (error) {
    panel.hidden = true;
    setSaveStatus(`Migration preview: ${error.message}`, "error");
  }
}

async function migrateLegacyProject() {
  const button = $("#migrate-project");
  button.disabled = true;
  try {
    const result = await operationalPost("/api/project/migrate", {
      confirmation: $("#migration-code").value,
    });
    state.project = result.project;
    state.payload.project = result.project;
    state.projectMigration = null;
    $("#migration-panel").hidden = true;
    renderRecoveryCenter();
    setSaveStatus("Folder-local Darkimiya project created and validated");
  } catch (error) {
    setSaveStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function openRecoveryCenter(loadError = "") {
  renderRecoveryCenter(loadError);
  loadProjectMigration();
  const dialog = $("#recovery-dialog");
  if (!dialog.open) dialog.showModal();
}

function switchWorkspace(mode) {
  state.workspaceMode = ["professional", "style", "develop", "verify"].includes(mode) ? mode : "review";
  const professional = state.workspaceMode === "professional";
  const style = state.workspaceMode === "style";
  const develop = state.workspaceMode === "develop";
  const verify = state.workspaceMode === "verify";
  const review = !professional && !style && !develop && !verify;
  $("#review-workspace").hidden = !review;
  $("#professional-workspace").hidden = !professional;
  $("#style-workspace").hidden = !style;
  $("#develop-workspace").hidden = !develop;
  $("#verify-workspace").hidden = !verify;
  $("#summary").hidden = professional || style || develop || verify;
  $("#inspector-toggle").hidden = !review;
  if (review) setInspectorOpen(state.inspectorOpen);
  const activeStage = professional ? "shortlist" : style ? "style"
    : develop ? "develop" : verify ? "verify" : "cull";
  document.querySelectorAll(".project-stage").forEach((button) => {
    const active = button.dataset.stage === activeStage;
    button.classList.toggle("active", active);
    button.toggleAttribute("aria-current", active);
  });
  $("#project-stage-note").textContent = {
    cull: "Cull", shortlist: "Shortlist", style: "Personal style",
    develop: "Develop", verify: "Verify",
  }[activeStage];
  if (professional) {
    renderShortlistWorkspace();
    const entry = activeShortlistEntry();
    if (entry) focusPreviews([entry.photo], [], "detail");
  }
  if (style) refreshJobs(true);
  if (style) loadStyleProfile();
  if (develop) loadDevelopmentWorkspace();
  if (verify) loadVerificationWorkspace();
}

async function loadVerificationWorkspace() {
  const payload = await (await fetch("/api/verification")).json();
  const certificate = payload.certificates?.at(-1);
  $("#verify-empty").hidden = Boolean(certificate);
  $("#verify-detail").hidden = !certificate;
  if (!certificate) {
    $("#verify-summary").textContent = "No verification certificate linked to this project.";
    return;
  }
  const judgment = certificate.judgment || certificate.result?.judgment || {};
  const satisfactory = judgment.satisfactory === true;
  $("#verify-summary").textContent = `${certificate.model || "Vision model"} · evidence certificate`;
  const badge = $("#verify-result-badge"); badge.textContent = satisfactory ? "Satisfactory" : "Needs attention";
  badge.classList.toggle("failed", !satisfactory);
  $("#verify-result-score").textContent = `${Math.round(Number(judgment.confidence || 0) * 100)}% confidence`;
  $("#verify-reasoning").textContent = judgment.reasoning || "No reasoning supplied.";
  const labels = {follows_suggestion: "Follows suggestion", preserves_subject: "Preserves subject",
    preserves_composition: "Preserves composition", photographic_quality: "Photographic quality"};
  const grid = $("#verify-score-grid"); grid.replaceChildren();
  for (const [field, label] of Object.entries(labels)) {
    const card = document.createElement("div"); card.className = "verify-score";
    const name = document.createElement("span"); name.textContent = label;
    const score = document.createElement("strong"); score.textContent = `${Math.round(Number(judgment[field] || 0) * 100)}%`;
    card.append(name, score); grid.append(card);
  }
  const list = $("#verify-concerns-list"); list.replaceChildren();
  for (const concern of (judgment.concerns || [])) { const item = document.createElement("li"); item.textContent = concern; list.append(item); }
  $("#verify-raw").textContent = JSON.stringify(certificate, null, 2);
}

function openAdjustmentDialog() {
  $("#adjustment-error").replaceChildren();
  $("#adjustment-note").value = "";
  ["midtones", "cyan", "shadows", "skin"].forEach((name) => { $("#adjust-" + name).checked = false; });
  $("#adjustment-dialog").showModal();
}

async function saveAdjustmentRevision() {
  const changes = [];
  for (const [id, label] of [["midtones", "lift midtones"], ["cyan", "reduce cyan/blue saturation"],
                              ["shadows", "recover shadow detail"], ["skin", "protect natural skin tones"]]) {
    if ($(`#adjust-${id}`).checked) changes.push({control: id, instruction: label});
  }
  if (!changes.length) { setSaveStatus("Select at least one bounded adjustment", "error"); return; }
  try {
    const current = state.project?.artifacts?.adjustments || [];
    const revision = {revision: current.length + 1, created_at: new Date().toISOString(),
      base_verification: state.project?.artifacts?.verifications?.at(-1) || null,
      changes, note: $("#adjustment-note").value.trim(), status: "draft"};
    state.project = await operationalPost("/api/project", {
      stage: "develop", artifacts: {adjustments: [...current, revision]},
    });
    $("#adjustment-dialog").close();
    setSaveStatus(`Adjustment revision ${revision.revision} saved; rerender required`);
  } catch (error) { const box = $("#adjustment-error"); box.textContent = error.message; }
}

async function loadDevelopmentWorkspace() {
  const response = await fetch("/api/development");
  const payload = await response.json();
  if (!response.ok) {
    setSaveStatus(payload.error || "Could not load development workspace", "error");
    return;
  }
  state.development = payload;
  const rendering = payload.rendering || {
    engine: "darktable", demosaic: "markesteijn-3-pass",
  };
  $("#develop-engine").value = rendering.engine;
  $("#develop-demosaic").value = rendering.demosaic;
  state.activeDevelopEngine ||= rendering.engine;
  const candidates = payload.candidates || [];
  if (!candidates.some((entry) => entry.photo === state.activeDevelopPhoto)) {
    state.activeDevelopPhoto = candidates[0]?.photo || null;
  }
  const activeEntry = candidates.find(
    (entry) => entry.photo === state.activeDevelopPhoto) || null;
  const availableRecipes = developmentRecipeDefinitions(activeEntry);
  if (!availableRecipes.some((recipe) => recipe.id === state.activeDevelopVariant)) {
    state.activeDevelopVariant = availableRecipes.find(
      (recipe) => developmentRecipeAvailable(activeEntry, recipe.id))?.id || "calibrated";
  }
  renderDevelopmentPhotoList();
  renderActiveDevelopmentPhoto();
  startDevelopmentCacheWarmup();
}

function developmentVariantsFor(photo) {
  const stem = String(photo || "").replace(/\.[^.]+$/, "");
  return (state.development?.variants || []).filter((item) =>
    item.source_photo === photo || (!item.source_photo &&
      String(item.path || "").split(/[\\/]/).pop()?.startsWith(`${stem}.`)));
}

const builtinDevelopmentRecipes = [
  {id: "calibrated", name: "Calibrated"},
  {id: "standard", name: "Standard"},
  {id: "signature", name: "Signature"},
  {id: "creative", name: "Creative"},
  {id: "personal", name: "Personal"},
];

function developmentRecipeDefinitions(entry) {
  const photo = entry?.photo || "";
  const imported = (state.development?.recipes || []).filter((recipe) =>
    recipe?.format === "darkimiya-portable-recipe-v1" && recipe.id &&
    (!recipe.photo || recipe.photo === photo)).map((recipe) => ({
      id: recipe.id, name: recipe.name || recipe.recipe?.title || "Imported recipe",
      portable: recipe,
    }));
  return [...builtinDevelopmentRecipes, ...imported];
}

function developmentRecipeDefinition(entry, style) {
  return developmentRecipeDefinitions(entry).find((recipe) => recipe.id === style);
}

function developmentRecipeAvailable(entry, style) {
  const definition = developmentRecipeDefinition(entry, style);
  return Boolean(entry && definition && style !== "original" &&
    (style === "calibrated" || definition.portable || entry[`${style}_recipe`]));
}

function developmentVariantStyle(item) {
  return String(item?.variant || item?.style || "render").split("-darktable-")[0];
}

function developmentVariantEngine(item) {
  return String(item?.variant || item?.style || "").includes("-darktable-")
    ? "darktable" : "default";
}

function developmentRenderFor(
  photo, style, engine = state.activeDevelopEngine || $("#develop-engine").value,
) {
  const matches = developmentVariantsFor(photo).filter((item) =>
    developmentVariantStyle(item) === style && developmentVariantEngine(item) === engine);
  if (engine === "darktable") {
    return [...matches].reverse().find((item) =>
      String(item.variant || item.style || "").includes("guided")) || matches.at(-1);
  }
  return matches.at(-1);
}

function developmentPreviewMaximum() {
  const canvas = $(".develop-canvas");
  const logical = Math.max(canvas?.clientWidth || 960, canvas?.clientHeight || 540);
  return Math.max(720, Math.min(2560, Math.ceil(logical * window.devicePixelRatio)));
}

function developmentPreviewKey(photo, style, engine, demosaic) {
  return `${photo}|${style}|${engine}|${demosaic}`;
}

function renderDevelopmentRenditionStrip(entry) {
  const strip = $("#develop-rendition-strip"); strip.replaceChildren();
  if (!entry) return;
  const demosaic = $("#develop-demosaic").value;
  const pending = [];
  const definitions = developmentRecipeDefinitions(entry);

  const rows = document.createElement("section"); rows.className = "develop-renderer-rows";
  for (const [engine, engineLabel] of [
    ["default", "Default renderer"], ["darktable", "Darktable renderer"],
  ]) {
    const row = document.createElement("section"); row.className = "develop-renderer-row";
    const heading = document.createElement("strong"); heading.className = "develop-renderer-label";
    heading.textContent = engineLabel; row.append(heading);
    for (const definition of definitions) {
      const style = definition.id;
      const item = developmentRenderFor(entry.photo, style, engine);
      const key = developmentPreviewKey(entry.photo, style, engine, demosaic);
      const previewUrl = state.developmentThumbnailUrls.get(key) ||
        state.developmentPreviewUrls.get(key);
      const ready = Boolean(item) || Boolean(previewUrl);
      const canPreview = developmentRecipeAvailable(entry, style);
      const button = document.createElement("button");
      button.type = "button"; button.className = "develop-rendition";
      button.classList.toggle("active", state.activeDevelopVariant === style &&
        state.activeDevelopEngine === engine);
      button.classList.toggle("pending", !ready);
      button.classList.toggle("generating", !ready && canPreview);
      button.dataset.variant = style; button.dataset.engine = engine;
      const image = document.createElement("img"); image.alt = ""; image.loading = "lazy";
      if (item) {
        image.src = `/api/development/image?path=${encodeURIComponent(item.path)}` +
          `&max=260&revision=${encodeURIComponent(item.sha256 || item.created_at || "render")}`;
      } else if (previewUrl) {
        image.src = previewUrl;
      } else {
        bindPreviewImage(image, entry.photo, "thumb");
      }
      const copy = document.createElement("span"); copy.className = "develop-rendition-copy";
      const name = document.createElement("strong"); name.textContent = definition.name;
      const status = document.createElement("span"); status.className = "develop-rendition-state";
      status.textContent = item ? "Ready" : previewUrl ? "Preview" :
        canPreview ? "Rendering" : "Unavailable";
      copy.append(name, status); button.append(image, copy);
      button.addEventListener("click", () => {
        state.developmentLastInteraction = Date.now();
        state.activeDevelopVariant = style;
        state.activeDevelopEngine = engine;
        strip.querySelectorAll(".develop-rendition.active").forEach(
          (item) => item.classList.remove("active"));
        button.classList.add("active");
        renderActiveDevelopmentPhoto({preserveRenditions: true});
        button.scrollIntoView({block: "nearest", inline: "nearest"});
      });
      row.append(button);
      if (!ready && canPreview) {
        pending.push({style, engine, key, button, image, status, endpoint:
          `/api/development/preview?photo=${encodeURIComponent(entry.photo)}` +
          `&style=${encodeURIComponent(style)}&engine=${encodeURIComponent(engine)}` +
          `&demosaic=${encodeURIComponent(demosaic)}&max=320`});
      }
    }
    rows.append(row);
  }
  strip.append(rows);
  void renderDevelopmentThumbnailPreviews(pending, entry.photo);
}

async function developmentThumbnailPreview(task) {
  if (state.developmentThumbnailUrls.has(task.key)) {
    return state.developmentThumbnailUrls.get(task.key);
  }
  let promise = state.developmentThumbnailTasks.get(task.key);
  if (!promise) {
    promise = (async () => {
      const response = await fetch(task.endpoint);
      if (!response.ok) {
        let message = "Local rendition preview failed";
        try { message = (await response.json()).error || message; } catch (_) { /* image error */ }
        throw new Error(message);
      }
      // Consume the generation response, but display through its stable local
      // URL. WebKit intermittently composited large batches of blob-backed
      // JPEG thumbnails as dark surfaces after replacing their placeholders.
      await response.blob();
      const url = task.endpoint;
      state.developmentThumbnailUrls.set(task.key, url);
      return url;
    })().finally(() => state.developmentThumbnailTasks.delete(task.key));
    state.developmentThumbnailTasks.set(task.key, promise);
  }
  return promise;
}

async function renderDevelopmentThumbnailPreviews(pending, photo) {
  const queues = new Map([
    ["default", pending.filter((task) => task.engine === "default")],
    ["darktable", pending.filter((task) => task.engine === "darktable")],
  ]);
  async function worker(queue) {
    while (queue.length) {
      const task = queue.shift();
      try {
        const url = await developmentThumbnailPreview(task);
        const current = state.activeDevelopPhoto === photo;
        if (!current || !task.button.isConnected) continue;
      unbindPreviewImage(task.image);
      task.image.src = url;
        task.button.classList.remove("pending", "generating");
        task.status.textContent = "Preview";
      } catch (_) {
        if (!task.button.isConnected) continue;
        task.button.classList.remove("generating");
        task.status.textContent = "Retry";
      }
    }
  }
  const workers = [];
  for (const queue of queues.values()) {
    for (let index = 0; index < Math.min(2, queue.length); index += 1) {
      workers.push(worker(queue));
    }
  }
  await Promise.all(workers);
}

function developmentCacheTasks() {
  const source = [];
  const thumbnails = [];
  const previews = [];
  const demosaic = $("#develop-demosaic").value;
  for (const entry of state.development?.candidates || []) {
    source.push({
      kind: "source", photo: entry.photo,
      endpoint: `/api/image?name=${encodeURIComponent(entry.photo)}&size=thumb`,
    });
    for (const definition of developmentRecipeDefinitions(entry)) {
      if (definition.source || !developmentRecipeAvailable(entry, definition.id)) continue;
      for (const engine of ["default", "darktable"]) {
        const base = `/api/development/preview?photo=${encodeURIComponent(entry.photo)}` +
          `&style=${encodeURIComponent(definition.id)}&engine=${engine}` +
          `&demosaic=${encodeURIComponent(demosaic)}`;
        thumbnails.push({
          kind: "thumbnail", photo: entry.photo, style: definition.id,
          engine, demosaic, endpoint: `${base}&max=320`,
        });
        previews.push({
          kind: "preview", photo: entry.photo, style: definition.id,
          engine, demosaic, endpoint: `${base}&max=960`,
        });
      }
    }
  }
  return [
    {name: "photograph thumbnails", tasks: source},
    {name: "treatment thumbnails", tasks: thumbnails},
    {name: "480-point previews", tasks: previews},
  ];
}

function renderDevelopmentCacheStatus(warmup = state.developmentCacheWarmup) {
  const region = $("#develop-cache-progress");
  if (!region || !warmup) return;
  region.hidden = false;
  const progress = $("#develop-cache-meter");
  progress.max = Math.max(1, warmup.total);
  progress.value = warmup.completed;
  const failed = warmup.failed ? ` · ${warmup.failed} unavailable` : "";
  $("#develop-cache-status").textContent = warmup.done
    ? `Preview cache ready · ${warmup.completed - warmup.failed} prepared${failed}`
    : `Preparing ${warmup.stage} in background · ${warmup.completed}/${warmup.total}${failed}`;
  region.classList.toggle("complete", warmup.done);
}

function waitForDevelopmentIdle() {
  return new Promise((resolve) => {
    const wait = () => {
      if (Date.now() - state.developmentLastInteraction < 700) {
        setTimeout(wait, 180);
      } else if ("requestIdleCallback" in window) {
        window.requestIdleCallback(resolve, {timeout: 700});
      } else {
        setTimeout(resolve, 80);
      }
    };
    wait();
  });
}

async function cacheDevelopmentTask(task, warmup) {
  await waitForDevelopmentIdle();
  if (warmup.cancelled) return;
  if (task.kind === "thumbnail") {
    const key = developmentPreviewKey(
      task.photo, task.style, task.engine, task.demosaic);
    const url = await developmentThumbnailPreview({...task, key});
    if (state.activeDevelopPhoto === task.photo) {
      const button = document.querySelector(
        `#develop-rendition-strip [data-variant="${task.style}"]` +
        `[data-engine="${task.engine}"]`);
      const image = button?.querySelector("img");
      const status = button?.querySelector(".develop-rendition-state");
      if (image) {
        unbindPreviewImage(image);
        image.src = url;
      }
      button?.classList.remove("pending", "generating");
      if (status) status.textContent = "Cached";
    }
    return;
  }
  let response = await fetch(task.endpoint);
  if (task.kind === "source") {
    for (let attempt = 0; response.status === 202 && attempt < 20; attempt += 1) {
      await response.body?.cancel();
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (warmup.cancelled) return;
      response = await fetch(task.endpoint);
    }
  }
  if (!response.ok) {
    let message = `cache request failed (${response.status})`;
    try { message = (await response.json()).error || message; } catch (_) { /* response */ }
    throw new Error(message);
  }
  await response.body?.cancel();
}

async function runDevelopmentCacheStage(stage, warmup) {
  warmup.stage = stage.name;
  renderDevelopmentCacheStatus(warmup);
  let cursor = 0;
  async function worker() {
    while (cursor < stage.tasks.length && !warmup.cancelled) {
      const task = stage.tasks[cursor++];
      try {
        await cacheDevelopmentTask(task, warmup);
      } catch (_) {
        warmup.failed += 1;
      } finally {
        warmup.completed += 1;
        renderDevelopmentCacheStatus(warmup);
      }
    }
  }
  await Promise.all(Array.from(
    {length: Math.min(2, stage.tasks.length)}, worker));
}

async function startDevelopmentCacheWarmup() {
  const stages = developmentCacheTasks();
  const signature = JSON.stringify({
    project: state.development?.project_sha256,
    demosaic: $("#develop-demosaic").value,
    tasks: stages.map((stage) => stage.tasks.map((task) => task.endpoint)),
  });
  const current = state.developmentCacheWarmup;
  if (current?.signature === signature && !current.cancelled) {
    renderDevelopmentCacheStatus(current);
    return;
  }
  if (current) current.cancelled = true;
  const warmup = {
    signature, completed: 0, failed: 0,
    total: stages.reduce((sum, stage) => sum + stage.tasks.length, 0),
    stage: "photograph thumbnails", done: false, cancelled: false,
  };
  state.developmentCacheWarmup = warmup;
  renderDevelopmentCacheStatus(warmup);
  for (const stage of stages) {
    if (warmup.cancelled) return;
    await runDevelopmentCacheStage(stage, warmup);
  }
  if (!warmup.cancelled) {
    warmup.done = true;
    warmup.stage = "previews";
    renderDevelopmentCacheStatus(warmup);
  }
}

async function importDevelopmentRecipe() {
  const button = $("#develop-import-recipe"); button.disabled = true;
  try {
    const payload = await operationalPost("/api/development/import-recipe", {});
    state.development = payload.development;
    state.activeDevelopVariant = payload.recipe.id;
    state.activeDevelopEngine ||= $("#develop-engine").value;
    renderActiveDevelopmentPhoto();
    startDevelopmentCacheWarmup();
    setSaveStatus(`Imported recipe: ${payload.recipe.name}`, "success");
  } catch (error) {
    if (!/cancelled/i.test(error.message)) setSaveStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function rendererComparisonsFor(photo) {
  return (state.development?.renderer_comparisons || []).filter(
    (item) => item.source_photo === photo);
}

async function saveDevelopmentRenderingPreference() {
  const rendering = {
    engine: $("#develop-engine").value,
    demosaic: $("#develop-demosaic").value,
  };
  try {
    state.project = await operationalPost("/api/project", {rendering});
    if (state.development) state.development.rendering = rendering;
    state.activeDevelopEngine = rendering.engine;
    renderActiveDevelopmentPhoto();
    startDevelopmentCacheWarmup();
    setSaveStatus(
      `${rendering.engine === "darktable" ? "Darktable" : "Darkimiya Default"} is now the project renderer`,
      "success");
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function exportActiveDevelopment() {
  const style = state.activeDevelopVariant || "original";
  const engine = state.activeDevelopEngine || $("#develop-engine").value;
  let item = style === "original" ? null : developmentRenderFor(
    state.activeDevelopPhoto, style, engine);
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  const definition = developmentRecipeDefinition(entry, style);
  const previewable = developmentRecipeAvailable(entry, style);
  if (!item && !previewable) {
    setSaveStatus("The selected rendition has no exportable recipe", "error");
    return;
  }
  const button = $("#develop-export-active");
  button.disabled = true; button.textContent = "Queueing…";
  showDevelopmentExportStatus(
    "working", "Preparing export",
    "The full-size image will be saved in this project's Darkimiya/Exports folder.");
  try {
    showDevelopmentExportStatus("working", "Preparing queued export",
      `${definition?.name || style} · ${engine === "darktable" ? "Darktable" : "Default renderer"}`);
    if (!item && definition?.portable) {
      setSaveStatus(`Rendering ${definition.name} at full size with ${engine === "darktable" ? "Darktable" : "the Default renderer"}…`);
      const rendered = await operationalPost("/api/development/render-portable", {
        photo: state.activeDevelopPhoto, style, engine,
        demosaic: $("#develop-demosaic").value,
      });
      state.development = rendered.development;
      item = rendered.render;
      renderActiveDevelopmentPhoto();
    }
    const filename =
      `${String(state.activeDevelopPhoto || "developed").replace(/\.[^.]+$/, "")}-${style}-${engine}.jpg`;
    const exportDirectory = state.development?.default_export_directory
      || `${String(state.development?.source_folder || "").replace(/\/$/, "")}/Darkimiya/Exports`;
    const destination = `${exportDirectory.replace(/\/$/, "")}/${filename}`;
    const reference = `${String(state.development?.source_folder || "").replace(/\/$/, "")}/${entry.photo}`;
    const source = entry.raw_files?.[0] || reference;
    const payload = await operationalPost("/api/jobs/add-delivery-export", {
      source, reference, directions: state.development.edit_directions_path,
      photo: entry.photo, style, engine,
      demosaic: $("#develop-demosaic").value,
      render: item?.path || "", destination,
      project: state.project?.path || state.payload?.project?.path || "",
    });
    state.jobs = payload;
    const job = [...payload.jobs].reverse().find((candidate) =>
      candidate.kind === "delivery_export" && candidate.photo === entry.photo &&
      candidate.style === style && candidate.engine === engine &&
      candidate.status === "queued");
    state.deliveryExportJobId = job?.id || null;
    showDevelopmentExportStatus(
      "working", "Export queued",
      "Rendering, delivery, and verification continue in Activity.");
    setSaveStatus("Image export added to Activity", "success");
    renderJobs();
    renderDeliveryExportJobStatus();
    refreshJobs(true);
  } catch (error) {
    setSaveStatus(error.message, "error");
    showDevelopmentExportStatus(
      /cancelled/i.test(error.message) ? "neutral" : "error",
      /cancelled/i.test(error.message) ? "Export cancelled" : "Export failed",
      error.message);
  } finally {
    button.disabled = Boolean(state.deliveryExportJobId);
    button.textContent = state.deliveryExportJobId ? "Export queued" : "Export";
  }
}

function showDevelopmentExportStatus(
  tone, title, message, reveal = false,
) {
  const region = $("#develop-export-status");
  region.hidden = false;
  region.classList.remove("working", "success", "error", "neutral");
  region.classList.add(tone);
  $("#develop-export-title").textContent = title;
  $("#develop-export-message").textContent = message;
  const progress = $("#develop-export-progress");
  if (tone === "success") {
    progress.max = 1; progress.value = 1;
  } else if (tone === "working") {
    progress.removeAttribute("value"); progress.max = 1;
  } else {
    progress.max = 1; progress.value = 0;
  }
  $("#develop-reveal-export").hidden = !reveal;
}

function renderSelectedDevelopment() {
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  if (developmentRecipeDefinition(entry, state.activeDevelopVariant)?.portable) {
    void renderPortableRecipeFull();
    return;
  }
  if ((state.activeDevelopEngine || $("#develop-engine").value) === "darktable") {
    queueFullResolutionDarktable();
  } else {
    queueGuidedDevelopment();
  }
}

async function renderPortableRecipeFull() {
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  const definition = developmentRecipeDefinition(entry, state.activeDevelopVariant);
  if (!entry || !definition?.portable) return;
  const engine = state.activeDevelopEngine || $("#develop-engine").value;
  $("#develop-guided-status").textContent =
    `Rendering ${definition.name} at full size with ${engine === "darktable" ? "Darktable" : "the Default renderer"}…`;
  try {
    const payload = await operationalPost("/api/development/render-portable", {
      photo: entry.photo, style: definition.id, engine,
      demosaic: $("#develop-demosaic").value,
    });
    state.development = payload.development;
    renderActiveDevelopmentPhoto();
    $("#develop-guided-status").textContent = "Full-size imported-recipe render ready";
  } catch (error) {
    $("#develop-guided-status").textContent = error.message;
  }
}

function renderDevelopmentPhotoList() {
  const candidates = state.development?.candidates || [];
  const query = $("#develop-search").value.trim().toLocaleLowerCase();
  const visible = candidates.filter((entry) =>
    !query || entry.photo.toLocaleLowerCase().includes(query));
  $("#develop-photo-count").textContent = String(candidates.length);
  const list = $("#develop-photo-list"); list.replaceChildren();
  for (const entry of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "develop-photo-item";
    button.classList.toggle("active", entry.photo === state.activeDevelopPhoto);
    button.dataset.photo = entry.photo;
    const image = document.createElement("img");
    image.alt = ""; image.loading = "lazy";
    bindPreviewImage(image, entry.photo, "thumb");
    const copy = document.createElement("span");
    const name = document.createElement("strong"); name.textContent = entry.photo;
    const detail = document.createElement("small");
    const rendered = developmentVariantsFor(entry.photo).length;
    detail.textContent = `${entry.raw_files?.length ? "RAW linked" : "JPEG source"} · ${rendered} rendered treatment${rendered === 1 ? "" : "s"}`;
    copy.append(name, detail); button.append(image, copy);
    button.addEventListener("click", () => {
      state.developmentLastInteraction = Date.now();
      state.activeDevelopPhoto = entry.photo;
      renderDevelopmentPhotoList();
      renderActiveDevelopmentPhoto();
      button.scrollIntoView({block: "nearest"});
    });
    list.append(button);
  }
  if (!candidates.length) {
    const empty = document.createElement("p"); empty.className = "muted develop-list-empty";
    empty.textContent = "Select photographs for edit-direction ideas in Professional Shortlist first.";
    list.append(empty);
  }
}

function applyDevelopmentComparison(entry) {
  const requested = state.developmentCompareMode || "treatment";
  const treatmentAvailable = !$("#develop-image").hidden;
  const mode = requested === "split" && !treatmentAvailable
    ? "original" : requested;
  const stage = $("#develop-ab-stage");
  stage.classList.toggle("split", mode === "split");
  $("#develop-original-pane").hidden = !entry || mode === "treatment";
  $("#develop-treatment-pane").hidden = mode === "original";
  if (entry) {
    const original = $("#develop-original-image");
    const source = `/api/image?name=${encodeURIComponent(entry.photo)}&size=detail`;
    if (!original.src.endsWith(source)) original.src = source;
    original.alt = `Original photograph ${entry.photo}`;
  }
  document.querySelectorAll(".develop-ab-button").forEach((button) =>
    button.classList.toggle("active", button.dataset.developView === mode));
  const treatmentLabel = $("#develop-treatment-pane figcaption");
  const definition = developmentRecipeDefinition(entry, state.activeDevelopVariant);
  treatmentLabel.textContent = `B · ${definition?.name || "Treatment"}`;
}

function setDevelopmentCompareMode(mode) {
  if (!["original", "treatment", "split"].includes(mode)) return;
  state.developmentCompareMode = mode;
  state.developmentLastInteraction = Date.now();
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  applyDevelopmentComparison(entry);
}

function renderActiveDevelopmentPhoto({preserveRenditions = false} = {}) {
  const payload = state.development || {};
  const entry = (payload.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  const active = state.activeDevelopVariant || "original";
  const activeEngine = state.activeDevelopEngine || $("#develop-engine").value;
  const definition = developmentRecipeDefinition(entry, active);
  const calibrated = active === "calibrated";
  const variants = developmentVariantsFor(state.activeDevelopPhoto);
  const item = active === "original" ? null : developmentRenderFor(
    state.activeDevelopPhoto, active, activeEngine);
  const comparison = rendererComparisonsFor(state.activeDevelopPhoto).at(-1);
  const engineView = active === "engines";
  const original = active === "original" && entry;
  const previewable = !engineView && (calibrated ||
    developmentRecipeAvailable(entry, active));
  const image = $("#develop-image");
  image.onload = null; image.onerror = null;
  image.hidden = !(original || item || previewable || (engineView && comparison));
  if (original) {
    bindPreviewImage(image, entry.photo, "detail");
    image.alt = `Original photograph ${entry.photo}`;
  } else if (item) {
    unbindPreviewImage(image);
    const revision = item.sha256 || item.created_at || "render";
    image.src = `/api/development/image?path=${encodeURIComponent(item.path)}&max=${developmentPreviewMaximum()}&revision=${encodeURIComponent(revision)}`;
    image.alt = `${active} treatment of ${entry?.photo || "photograph"}`;
  } else if (engineView && comparison) {
    unbindPreviewImage(image);
    const revision = comparison.contact_sheet_sha256 || comparison.created_at;
    image.src = `/api/development/image?path=${encodeURIComponent(comparison.contact_sheet)}&revision=${encodeURIComponent(revision)}`;
    image.alt = `Built-in and darktable comparison for ${entry?.photo || "photograph"}`;
  } else if (previewable) {
    unbindPreviewImage(image);
    const engine = activeEngine;
    const demosaic = $("#develop-demosaic").value;
    // 960 device pixels is a crisp 480-point preview on a Retina display and
    // exactly matches the second background-cache tier.
    const maximum = 960;
    $("#develop-guided-status").textContent = "Rendering a bounded local preview…";
    image.src = `/api/development/preview?photo=${encodeURIComponent(entry.photo)}` +
      `&style=${encodeURIComponent(active)}&engine=${encodeURIComponent(engine)}` +
      `&demosaic=${encodeURIComponent(demosaic)}&max=${maximum}`;
    image.alt = `${active} local preview of ${entry.photo}`;
    image.onload = () => {
      if (state.activeDevelopPhoto !== entry.photo ||
          state.activeDevelopVariant !== active ||
          state.activeDevelopEngine !== engine) return;
      const key = developmentPreviewKey(entry.photo, active, engine, demosaic);
      state.developmentPreviewUrls.set(key, image.src);
      const thumbnail = document.querySelector(
        `#develop-rendition-strip [data-variant="${active}"][data-engine="${engine}"] img`);
      if (thumbnail) thumbnail.src = image.src;
      $("#develop-guided-status").textContent =
        `${engine === "darktable" ? "Darktable" : "Default"} local preview ready · export will render full size`;
    };
    image.onerror = () => {
      $("#develop-guided-status").textContent =
        "Local preview failed; the full-resolution source and recipe were not changed.";
    };
  } else {
    unbindPreviewImage(image);
    image.removeAttribute("src");
  }
  if (!preserveRenditions) renderDevelopmentRenditionStrip(entry);
  $("#develop-empty").hidden = Boolean(
    original || item || previewable || (engineView && comparison));
  $("#develop-empty-title").textContent = entry
    ? engineView ? "Renderer comparison not generated" :
      `${active[0].toUpperCase() + active.slice(1)} treatment not rendered`
    : "No photographs selected for development";
  $("#develop-empty-message").textContent = entry
    ? "The guided recipe is ready. Render this treatment to compare it with the original."
    : "Return to Professional Shortlist and select photographs for edit-direction ideas.";
  const label = active === "original" ? "Original" : engineView ?
    "Built-in and darktable" :
    `${definition?.name || active} · ${activeEngine === "darktable" ? "Darktable" : "Default"}`;
  $("#develop-variant-title").textContent = entry ? `${entry.photo} · ${label}` : label;
  $("#develop-summary").textContent = entry
    ? `${payload.candidates.length} photographs selected for development · ${variants.length} treatment${variants.length === 1 ? "" : "s"} rendered for this photograph`
    : "No photographs are currently selected for development.";

  const metadata = $("#develop-metadata"); metadata.replaceChildren();
  for (const [key, value] of [
    ["Source", entry?.photo || "—"],
    ["RAW", entry?.raw_files?.[0] || "No matching RAW linked"],
    ["Status", original ? "Original reference" : engineView && comparison ?
      "Visual and technical comparison ready" : item ? "Full-size render ready" :
        previewable ? "Local preview; full-size render pending" : "Guidance ready"],
  ]) {
    const dt = document.createElement("dt"); dt.textContent = key;
    const dd = document.createElement("dd"); dd.textContent = value;
    metadata.append(dt, dd);
  }
  if (engineView && comparison) {
    const target = $("#develop-guidance"); target.replaceChildren();
    const heading = document.createElement("strong"); heading.textContent = "Paired renderer research";
    const notice = document.createElement("p"); notice.textContent = comparison.research_notice;
    const technical = document.createElement("pre");
    technical.textContent = JSON.stringify(comparison.technical_comparison, null, 2);
    target.append(heading, notice, technical);
    $("#develop-recipe").textContent = "{}";
  } else {
    renderDevelopmentGuidance(entry, active, item);
  }
  const canRender = developmentRecipeAvailable(entry, active);
  $("#develop-guided-render").disabled = !canRender;
  const selectedEngine = activeEngine;
  $("#develop-guided-render").textContent = item
    ? "Render new revision"
    : `Render with ${selectedEngine === "darktable" ? "Darktable" : "Darkimiya Default"}`;
  const deliveryJob = state.jobs?.jobs?.find(
    (candidate) => candidate.id === state.deliveryExportJobId);
  const deliveryActive = Boolean(deliveryJob &&
    deliveryJob.photo === entry?.photo && deliveryJob.style === active &&
    deliveryJob.engine === activeEngine &&
    ["queued", "running", "stopping", "detached"].includes(deliveryJob.status));
  $("#develop-export-active").disabled = !(item || previewable) || deliveryActive;
  $("#develop-export-active").textContent = deliveryActive ? "Export queued" : "Export";
  $("#develop-compare-renderers").disabled = !Boolean(
    entry && item && active !== "original" && active !== "engines");
  $("#develop-render-full-darktable").disabled = !canRender;
  $("#develop-reference").value = entry
    ? `${String(payload.source_folder || "").replace(/\/$/, "")}/${entry.photo}` : "";
  if (entry?.raw_files?.length) {
    $("#develop-baseline").placeholder = `Decode ${entry.raw_files[0]} to a baseline TIFF`;
  }
  applyDevelopmentComparison(entry);
}

async function queueRendererComparison() {
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  const style = state.activeDevelopVariant;
  const item = [...developmentVariantsFor(state.activeDevelopPhoto)].reverse().find(
    (variant) => (variant.variant || variant.style) === style);
  if (!entry || !item || ["original", "engines"].includes(style)) return;
  const reference = `${String(state.development.source_folder || "").replace(/\/$/, "")}/${entry.photo}`;
  const source = entry.raw_files?.[0] || reference;
  const button = $("#develop-compare-renderers"); button.disabled = true;
  $("#develop-guided-status").textContent = "Preparing isolated paired render…";
  try {
    const payload = await operationalPost("/api/jobs/add-renderer-comparison", {
      source, reference, directions: state.development.edit_directions_path,
      photo: entry.photo, style, opencull_render: item.path,
      demosaic: $("#develop-demosaic").value,
      project: state.project?.path || state.payload?.project?.path || "",
    });
    state.jobs = payload;
    const job = [...payload.jobs].reverse().find((candidate) =>
      candidate.kind === "renderer_comparison" && candidate.photo === entry.photo &&
      candidate.style === style && candidate.status === "queued");
    state.comparisonJobId = job?.id || null;
    renderComparisonJobStatus(); refreshJobs(true);
  } catch (error) {
    $("#develop-guided-status").textContent = error.message;
    button.disabled = false;
  }
}

function renderComparisonJobStatus() {
  const job = state.jobs?.jobs?.find((item) => item.id === state.comparisonJobId);
  if (!job) return;
  const matches = [...String(job.log_tail || "").matchAll(
    /COMPARE_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)/g)];
  const latest = matches.at(-1);
  const percent = latest ? Number(latest[1]) : job.status === "completed" ? 100 : 0;
  $("#develop-guided-status").textContent =
    `${latest?.[2]?.trim() || job.message} · ${percent}%`;
  if (job.status === "failed") {
    $("#develop-compare-renderers").disabled = false;
  } else if (job.status === "completed" &&
      $("#develop-guided-status").dataset.loadedComparison !== job.id) {
    $("#develop-guided-status").dataset.loadedComparison = job.id;
    state.activeDevelopPhoto = job.photo || state.activeDevelopPhoto;
    state.activeDevelopVariant = "engines";
    loadDevelopmentWorkspace();
  }
}

async function queueFullResolutionDarktable() {
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  const style = state.activeDevelopVariant;
  if (!entry || ["original", "engines"].includes(style)) return;
  const reference = `${String(state.development.source_folder || "").replace(/\/$/, "")}/${entry.photo}`;
  const source = entry.raw_files?.[0] || reference;
  const button = $("#develop-render-full-darktable");
  button.disabled = true;
  $("#develop-guided-render").disabled = true;
  $("#develop-guided-status").textContent =
    "Preparing full-resolution darktable rendering…";
  try {
    const payload = await operationalPost("/api/jobs/add-renderer-export", {
      source, reference, directions: state.development.edit_directions_path,
      photo: entry.photo, style, demosaic: $("#develop-demosaic").value,
      project: state.project?.path || state.payload?.project?.path || "",
    });
    state.jobs = payload;
    const job = [...payload.jobs].reverse().find((candidate) =>
      candidate.kind === "renderer_export" && candidate.photo === entry.photo &&
      candidate.style === style && candidate.status === "queued");
    state.rendererExportJobId = job?.id || null;
    renderRendererExportJobStatus();
    refreshJobs(true);
  } catch (error) {
    $("#develop-guided-status").textContent = error.message;
    button.disabled = false;
    $("#develop-guided-render").disabled = false;
  }
}

function renderRendererExportJobStatus() {
  const job = state.jobs?.jobs?.find(
    (item) => item.id === state.rendererExportJobId);
  if (!job) return;
  if (job.photo !== state.activeDevelopPhoto ||
      job.style !== state.activeDevelopVariant) return;
  const matches = [...String(job.log_tail || "").matchAll(
    /RENDER_EXPORT_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)/g)];
  const latest = matches.at(-1);
  const percent = latest ? Number(latest[1]) : job.status === "completed" ? 100 : 0;
  $("#develop-guided-status").textContent =
    `${latest?.[2]?.trim() || job.message} · ${percent}%`;
  const button = $("#develop-render-full-darktable");
  if (job.status === "failed") {
    const failure = [...String(job.log_tail || "").matchAll(
      /(?:ValueError|RuntimeError|Error):\s*([^\r\n]+)/g)].at(-1)?.[1];
    $("#develop-guided-status").textContent =
      `Rendering failed at ${percent}% · ${failure || job.message}`;
    button.disabled = false;
    $("#develop-guided-render").disabled = false;
    button.textContent = "Darktable-Powered Render";
    state.rendererExportJobId = null;
  } else if (job.status === "completed" &&
      $("#develop-guided-status").dataset.loadedRendererExport !== job.id) {
    $("#develop-guided-status").dataset.loadedRendererExport = job.id;
    $("#develop-guided-status").textContent =
      "Full-resolution Darktable render is ready · 100%";
    button.disabled = false;
    $("#develop-guided-render").disabled = false;
    button.textContent = "Darktable-Powered Render";
    state.rendererExportJobId = null;
    loadDevelopmentWorkspace();
  } else {
    button.disabled = ["queued", "running", "stopping", "detached"].includes(job.status);
  }
}

function renderDeliveryExportJobStatus() {
  const job = state.jobs?.jobs?.find(
    (item) => item.id === state.deliveryExportJobId);
  if (!job) return;
  const matches = [...String(job.log_tail || "").matchAll(
    /EXPORT_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)/g)];
  const latest = matches.at(-1);
  const percent = latest ? Number(latest[1]) : job.status === "completed" ? 100 : 0;
  const current = job.photo === state.activeDevelopPhoto &&
    job.style === state.activeDevelopVariant &&
    job.engine === (state.activeDevelopEngine || $("#develop-engine").value);
  if (current) {
    showDevelopmentExportStatus(
      "working", job.status === "queued" ? "Export queued" : "Exporting in background",
      `${latest?.[2]?.trim() || job.message} · ${percent}%`);
    const progress = $("#develop-export-progress");
    progress.max = 100; progress.value = percent;
    $("#develop-export-active").disabled = true;
    $("#develop-export-active").textContent = "Export queued";
  }
  if (job.status === "completed") {
    state.lastDevelopmentExportPath = job.export_destination || null;
    if (current) {
      showDevelopmentExportStatus(
        "success", "Export complete",
        job.export_destination || "The verified image is ready in Darkimiya/Exports.",
        Boolean(job.export_destination));
      $("#develop-export-active").disabled = false;
      $("#develop-export-active").textContent = "Export";
    }
    state.deliveryExportJobId = null;
  } else if (["failed", "cancelled", "paused"].includes(job.status)) {
    if (current) {
      showDevelopmentExportStatus(
        job.status === "failed" ? "error" : "neutral",
        job.status === "failed" ? "Export failed" : "Export stopped",
        job.message);
      $("#develop-export-active").disabled = false;
      $("#develop-export-active").textContent = "Export";
    }
    state.deliveryExportJobId = null;
  }
}

async function queueGuidedDevelopment() {
  const entry = (state.development?.candidates || []).find(
    (candidate) => candidate.photo === state.activeDevelopPhoto);
  const style = state.activeDevelopVariant;
  if (!entry || style === "original") return;
  const reference = `${String(state.development.source_folder || "").replace(/\/$/, "")}/${entry.photo}`;
  const source = entry.raw_files?.[0] || reference;
  const button = $("#develop-guided-render"); button.disabled = true;
  $("#develop-guided-status").textContent = entry.raw_files?.length
    ? "Preparing the linked RAW file…" : "Preparing the JPEG source…";
  try {
    const payload = await operationalPost("/api/jobs/add-development-pipeline", {
      source, reference, directions: state.development.edit_directions_path,
      photo: entry.photo, style,
      project: state.project?.path || state.payload?.project?.path || "",
    });
    state.jobs = payload;
    const job = [...payload.jobs].reverse().find((item) =>
      item.kind === "development_pipeline" && item.photo === entry.photo &&
      item.style === style && item.status === "queued");
    state.developmentJobId = job?.id || null;
    renderDevelopmentJobStatus();
    refreshJobs(true);
  } catch (error) {
    $("#develop-guided-status").textContent = error.message;
    button.disabled = false;
  }
}

function renderDevelopmentJobStatus() {
  const job = state.jobs?.jobs?.find((item) => item.id === state.developmentJobId);
  if (!job) return;
  const matches = [...String(job.log_tail || "").matchAll(
    /DEVELOP_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)/g)];
  const latest = matches.at(-1);
  const percent = latest ? Number(latest[1]) : job.status === "completed" ? 100 : 0;
  const stage = latest?.[2]?.trim() || job.message;
  $("#develop-guided-status").textContent = `${stage} · ${percent}%`;
  $("#develop-guided-render").disabled = [
    "queued", "running", "stopping", "detached", "completed"].includes(job.status);
  if (job.status === "failed") {
    $("#develop-guided-render").disabled = false;
    $("#develop-guided-render").textContent = "Try rendering again";
  } else if (job.status === "completed" &&
      $("#develop-guided-status").dataset.loadedJob !== job.id) {
    $("#develop-guided-status").dataset.loadedJob = job.id;
    state.activeDevelopPhoto = job.photo || state.activeDevelopPhoto;
    state.activeDevelopVariant = job.style || state.activeDevelopVariant;
    $("#develop-guided-status").textContent =
      "Guided treatment is ready · loading image…";
    loadDevelopmentWorkspace();
  }
}

function renderDevelopmentGuidance(entry, style, rendered) {
  const target = $("#develop-guidance"); target.replaceChildren();
  if (!entry || style === "original") {
    const note = document.createElement("p");
    note.textContent = "Choose a treatment above to inspect its guided edit.";
    target.append(note); $("#develop-recipe").textContent = "{}"; return;
  }
  if (style === "calibrated") {
    const heading = document.createElement("strong");
    heading.textContent = "Camera-matched RAW baseline";
    const purpose = document.createElement("p");
    purpose.className = "develop-guidance-intent";
    purpose.textContent = "A neutral technical baseline matched to the reference JPEG before creative editing.";
    const direction = document.createElement("p");
    direction.textContent = "No Standard, Signature, Creative, or Personal adjustments are applied.";
    const status = document.createElement("span");
    status.textContent = rendered ? "Rendered result linked" : "Ready to render";
    target.append(heading, purpose, direction, status);
    $("#develop-recipe").textContent = "{}";
    return;
  }
  const definition = developmentRecipeDefinition(entry, style);
  const portable = definition?.portable;
  const title = portable?.name || entry[`${style}_title`] || `${style} treatment`;
  const intent = portable?.recipe?.intent || entry[`${style}_intent`] ||
    "Reusable project recipe.";
  const instructions = portable?.recipe?.instructions ||
    entry[`${style}_instructions`] || (portable
      ? `${portable.recipe.operations.length} executable adjustment operations imported from ${portable.source_path || "a recipe file"}.`
      : "No instructions were supplied.");
  const heading = document.createElement("strong"); heading.textContent = title;
  const purpose = document.createElement("p"); purpose.className = "develop-guidance-intent";
  purpose.textContent = intent;
  const direction = document.createElement("p"); direction.textContent = instructions;
  const status = document.createElement("span");
  status.textContent = rendered ? "Rendered result linked" : "Guided recipe ready to render";
  target.append(heading, purpose, direction, status);
  const rawRecipe = portable?.recipe || entry[`${style}_recipe`];
  try {
    $("#develop-recipe").textContent = JSON.stringify(
      typeof rawRecipe === "string" ? JSON.parse(rawRecipe) : rawRecipe || {}, null, 2);
  } catch (_) {
    $("#develop-recipe").textContent = String(rawRecipe || "{}");
  }
}

async function queueDevelopmentRender() {
  try {
    const payload = await operationalPost("/api/jobs/add-development-render", {
      baseline: $("#develop-baseline").value.trim(), recipe: $("#develop-recipe-path").value.trim(),
      reference_jpeg: $("#develop-reference").value.trim(), output_dir: $("#develop-output-dir").value.trim(),
      project: state.project?.path || state.payload?.project?.path || "",
      allow_incomplete: true,
    });
    state.jobs = payload; setSaveStatus("Development render added to Queue"); refreshJobs();
  } catch (error) { $("#develop-render-error").textContent = error.message; }
}

async function loadStyleProfile() {
  try {
    const response = await fetch("/api/style-profile");
    const payload = await response.json();
    const empty = $("#style-profile-empty");
    const detail = $("#style-profile-detail");
    empty.hidden = payload.available; detail.hidden = !payload.available;
    if (!payload.available) {
      $("#style-profile-summary").textContent = payload.reason || "No profile selected.";
      return;
    }
    const value = payload.profile;
    const profile = value.profile || {};
    $("#style-profile-summary").textContent =
      `${profile.profile_name} · revision ${value.revision} · ${value.examples?.length || 0} example photos`;
    const labels = {
      visual_signature: "Visual signature", tonal_preferences: "Tonal preferences",
      contrast_preferences: "Contrast", color_preferences: "Color palette",
      white_balance_preferences: "White balance", saturation_preferences: "Saturation",
      highlight_shadow_preferences: "Highlights and shadows", texture_detail_preferences: "Texture and detail",
      composition_preferences: "Composition", subject_and_skin_preferences: "Subject and skin",
      scene_adaptation: "Scene adaptation", preferred_adjustments: "Preferred adjustments",
      avoid_or_guardrails: "Guardrails", professional_refinement: "Professional refinement",
    };
    const target = $("#style-profile-cards"); target.replaceChildren();
    for (const [field, label] of Object.entries(labels)) {
      const card = document.createElement("article"); card.className = "style-profile-card";
      const heading = document.createElement("h3"); heading.textContent = label;
      const text = document.createElement("p"); text.textContent = profile[field] || "—";
      card.append(heading, text); target.append(card);
    }
    const history = Array.isArray(value.history) ? value.history : [];
    const revisionSummary = history.length
      ? history.map((item, index) => {
          const mode = item.mode || "update";
          const when = item.updated_at || "unknown time";
          const count = item.example_count ?? "—";
          return `Revision ${index + 1} · ${mode} · ${when} · ${count} example${count === 1 ? "" : "s"}`;
        }).join("\n")
      : "No revision history yet.";
    $("#style-profile-history").textContent = revisionSummary + "\n\n" + JSON.stringify({
      path: payload.path, confidence: profile.confidence,
      examples: value.examples, history,
    }, null, 2);
  } catch (error) { setSaveStatus(error.message, "error"); }
}

async function setProjectStage(stage) {
  const labels = {cull: "Cull", shortlist: "Shortlist", style: "Personal style",
    develop: "Develop", verify: "Verify"};
  if (!["cull", "shortlist", "style", "develop", "verify"].includes(stage)) {
    setSaveStatus(`${labels[stage]} workspace is coming in a later phase`, "error");
    return;
  }
  // Navigation must remain local and immediate. Persisting the last stage is
  // useful project metadata, but it must never block WebKit from repainting.
  if (stage === "style") switchWorkspace("style");
  else if (stage === "develop") switchWorkspace("develop");
  else if (stage === "verify") switchWorkspace("verify");
  else if (stage === "shortlist") switchWorkspace("professional");
  else switchWorkspace("review");
  try {
    const project = await operationalPost("/api/project", {stage});
    state.project = project;
  } catch (error) { setSaveStatus(error.message, "error"); }
}

function shortlistHumanEntry(photo) {
  return state.shortlistReview?.entries?.[photo] || null;
}

function effectiveShortlistEntry(entry) {
  const human = shortlistHumanEntry(entry.photo);
  const reviewed = Boolean(human?.reviewed);
  const externalRaw = state.rawSource?.matches?.[entry.photo] || [];
  return {
    ...entry,
    raw_files: [...new Set([...(entry.raw_files || []), ...externalRaw])],
    effectiveTier: reviewed ? human.tier : entry.tier,
    editRaw: reviewed
      ? human.edit_raw : ["exceptional", "strong"].includes(entry.tier),
    interesting: Boolean(human?.interesting),
    humanReviewed: reviewed,
    humanNote: human?.note || "",
  };
}

function filteredShortlistEntries() {
  if (!state.shortlist) return [];
  const tier = $("#shortlist-tier-filter").value;
  const raw = $("#shortlist-raw-filter").value;
  const review = $("#shortlist-review-filter").value;
  const query = state.shortlistQuery.toLowerCase();
  return state.shortlist.entries.map(effectiveShortlistEntry).filter((entry) => {
    if (tier !== "all" && entry.effectiveTier !== tier) return false;
    if (raw === "available" && !entry.raw_files.length) return false;
    if (raw === "missing" && entry.raw_files.length) return false;
    if (review === "reviewed" && !entry.humanReviewed) return false;
    if (review === "unreviewed" && entry.humanReviewed) return false;
    if (review === "edit" && !entry.editRaw) return false;
    if (query && ![
      entry.photo, entry.effectiveTier, entry.rationale, entry.cluster_id,
      ...entry.raw_files,
    ].join(" ").toLowerCase().includes(query)) return false;
    return true;
  });
}

function shortlistListItem(entry) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "shortlist-item";
    button.dataset.photo = entry.photo;
    if (entry.photo === state.activeShortlistPhoto) button.classList.add("active");
    const image = document.createElement("img");
    image.className = "shortlist-thumb";
    image.alt = "";
    image.loading = "lazy";
    bindPreviewImage(image, entry.photo, "thumb");
    const copy = document.createElement("span");
    copy.className = "shortlist-item-copy";
    const name = document.createElement("strong");
    name.textContent = entry.photo.split("/").at(-1);
    const detail = document.createElement("span");
    detail.textContent = `${entry.effectiveTier} · ${Math.round(entry.score)} · ${
      entry.raw_files.length ? "RAW" : "JPEG only"}${
      entry.humanReviewed ? " · reviewed" : ""}${
      entry.interesting ? " · edit ideas" : ""}`;
    const direction = state.editDirections?.entries?.find(
      (item) => item.photo === entry.photo);
    if (direction?.kimiya_validation?.status === "rejected") {
      detail.textContent += " · Kimiya rejected";
      button.classList.add("validation-rejected");
    }
    copy.append(name, detail);
    const marker = document.createElement("span");
    marker.className = `tier-dot ${entry.effectiveTier}`;
    marker.title = entry.effectiveTier;
    const rank = document.createElement("span");
    rank.className = "shortlist-rank";
    rank.textContent = `#${entry.rank}`;
    const end = document.createElement("span");
    end.style.display = "grid";
    end.style.justifyItems = "center";
    end.style.gap = "7px";
    end.append(rank, marker);
    button.append(image, copy, end);
    button.addEventListener("click", () => showShortlistEntry(entry.photo));
    return button;
}

function renderShortlistList() {
  state.shortlistVisible = filteredShortlistEntries();
  const target = $("#shortlist-list");
  target.replaceChildren();
  for (const entry of state.shortlistVisible) {
    const button = shortlistListItem(entry);
    target.append(button);
  }
}

function refreshShortlistListItem(photo) {
  const entry = state.shortlistVisible.find((item) => item.photo === photo);
  const current = [...document.querySelectorAll("#shortlist-list .shortlist-item")]
    .find((item) => item.dataset.photo === photo);
  if (!entry || !current) return false;
  current.replaceWith(shortlistListItem(entry));
  return true;
}

function activeShortlistEntry() {
  return state.shortlist?.entries.find(
    (entry) => entry.photo === state.activeShortlistPhoto) || null;
}

function updateShortlistPreviewAspect() {
  const image = $("#shortlist-image");
  const stage = $("#shortlist-open-viewer");
  const width = Number(image.naturalWidth || 0);
  const height = Number(image.naturalHeight || 0);
  if (!width || !height) return;
  stage.style.setProperty("--preview-aspect-ratio", `${width} / ${height}`);
  stage.classList.toggle("preview-portrait", height > width);
}

function renderShortlistDetail() {
  const source = activeShortlistEntry();
  $("#shortlist-empty").hidden = Boolean(source);
  $("#shortlist-detail").hidden = !source;
  if (!source) return;
  const entry = effectiveShortlistEntry(source);
  const position = state.shortlistVisible.findIndex(
    (item) => item.photo === entry.photo);
  $("#shortlist-position").textContent =
    `RANK ${entry.rank} · ${position + 1} OF ${state.shortlistVisible.length} IN CURRENT VIEW`;
  $("#shortlist-title").textContent = entry.photo;
  $("#shortlist-tier").textContent = entry.effectiveTier;
  $("#shortlist-tier").className = `tier-badge ${entry.effectiveTier}`;
  $("#shortlist-score").textContent = `${Math.round(entry.score)}/100`;
  $("#shortlist-raw-status").textContent = entry.raw_files.length
    ? `${entry.raw_files.length} RAW companion${entry.raw_files.length === 1 ? "" : "s"}`
    : "No RAW companion";
  $("#shortlist-human-status").textContent = entry.humanReviewed
    ? "Human reviewed" : "AI recommendation";
  const image = $("#shortlist-image");
  const stage = $("#shortlist-open-viewer");
  const previewChanged = image.dataset.previewName !== entry.photo;
  stage.style.setProperty("--preview-aspect-ratio", "3 / 2");
  stage.classList.remove("preview-portrait");
  image.alt = entry.photo;
  bindPreviewImage(image, entry.photo, "detail");
  image.onload = updateShortlistPreviewAspect;
  const previewIsReady = !previewChanged && image.complete && image.naturalWidth > 0;
  $("#shortlist-open-viewer .image-loading").hidden = previewIsReady;
  $("#shortlist-open-viewer .image-error").hidden = true;
  $("#shortlist-rationale").textContent = entry.rationale;
  $("#shortlist-cluster").textContent = entry.cluster_id;
  $("#shortlist-raw-files").textContent =
    entry.raw_files.join(", ") || "No associated RAW file";
  $("#shortlist-confidence").textContent =
    `${Math.round(Number(entry.confidence || 0) * 100)}%`;
  $("#shortlist-human-tier").value = entry.effectiveTier;
  $("#shortlist-edit-raw").checked = entry.editRaw;
  $("#shortlist-interesting").checked = entry.interesting;
  $("#shortlist-reviewed").checked = entry.humanReviewed;
  $("#shortlist-note").value = entry.humanNote;
  const compatible = !state.shortlistReview?.stale;
  for (const control of [
    $("#shortlist-human-tier"), $("#shortlist-edit-raw"),
    $("#shortlist-interesting"),
    $("#shortlist-reviewed"), $("#shortlist-note"),
    $("#save-shortlist-review"), $("#save-shortlist-next"),
  ]) control.disabled = !compatible;
  $("#undo-shortlist-review").disabled =
    !compatible || !state.shortlistReview?.history?.length;
  $("#previous-shortlist").disabled = position <= 0;
  $("#next-shortlist").disabled =
    position < 0 || position >= state.shortlistVisible.length - 1;
  const assessment = $("#shortlist-assessment");
  assessment.replaceChildren();
  for (const [field, label] of Object.entries(professionalAssessmentLabels)) {
    const card = document.createElement("article");
    card.className = "professional-assessment-card";
    const heading = document.createElement("h4");
    heading.textContent = label;
    const text = document.createElement("p");
    text.textContent = entry.assessment?.[field] || "Not assessed.";
    card.append(heading, text);
    assessment.append(card);
  }
  const warnings = $("#shortlist-warnings");
  warnings.replaceChildren();
  for (const message of entry.warnings || []) {
    const item = document.createElement("div");
    item.className = "alert";
    item.textContent = message;
    warnings.append(item);
  }
  $("#shortlist-raw-json").textContent = JSON.stringify(source, null, 2);
  renderEditDirections(entry.photo);
  if (!previewIsReady) focusPreviews([entry.photo], [], "detail");
}

function renderEditDirections(photo) {
  const section = $("#edit-directions-section");
  const entry = state.editDirections?.entries?.find(
    (item) => item.photo === photo);
  section.hidden = !entry;
  if (!entry) return;
  const validation = $("#edit-direction-validation");
  const rejected = entry.kimiya_validation?.status === "rejected";
  validation.hidden = !rejected;
  $("#edit-direction-validation-message").textContent = rejected
    ? entry.kimiya_validation.message
    : "";
  $("#regenerate-edit-direction").dataset.photo = rejected ? photo : "";
  $("#edit-direction-scene").textContent = entry.scene_reading;
  $("#edit-direction-guardrails").textContent = entry.guardrails;
  const target = $("#edit-direction-options");
  target.replaceChildren();
  for (const kind of ["standard", "signature", "creative", "personal"]) {
    const card = document.createElement("article");
    card.className = `professional-assessment-card edit-direction-card ${kind}`;
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = kind === "standard"
      ? "STANDARD PROFESSIONAL"
      : kind === "signature" ? "SIGNATURE STYLE"
      : kind === "creative" ? "TASTEFUL CREATIVE"
      : "YOUR PERSONAL STYLE · REFINED";
    const title = document.createElement("h4");
    title.textContent = entry[`${kind}_title`];
    const intent = document.createElement("p");
    intent.className = "direction-intent";
    intent.textContent = entry[`${kind}_intent`];
    const instructions = document.createElement("p");
    instructions.textContent = entry[`${kind}_instructions`];
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Editing recipe";
    const recipe = JSON.parse(entry[`${kind}_recipe`] || "{}");
    const recipeBody = document.createElement("div");
    recipeBody.className = "direction-recipe";
    for (const [section, steps] of Object.entries(recipe)) {
      const heading = document.createElement("h5");
      heading.textContent = section.replaceAll("_", " ");
      const list = document.createElement("ol");
      for (const step of steps) {
        const item = document.createElement("li");
        item.textContent = step;
        list.append(item);
      }
      recipeBody.append(heading, list);
    }
    details.append(summary, recipeBody);
    card.append(eyebrow, title, intent, instructions, details);
    target.append(card);
  }
}

function openSemanticVerification() {
  const entry = state.editDirections?.entries?.find(
    (item) => item.photo === state.activeShortlistPhoto);
  if (!entry) {
    setSaveStatus("Load edit directions for this photo first", "error");
    return;
  }
  $("#semantic-developed").value = "";
  $("#semantic-thumbnail").value = "";
  $("#semantic-suggestion").value =
    `${entry.standard_title}: ${entry.standard_intent}\n${entry.standard_instructions}`;
  const select = $("#semantic-provider");
  select.replaceChildren();
  for (const profile of state.providers?.profiles || []) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = providerOptionLabel(profile);
    select.append(option);
  }
  $("#semantic-model").value = "";
  $("#semantic-consensus").checked = false;
  $("#semantic-verification-error").replaceChildren();
  $("#semantic-verification-dialog").showModal();
}

async function openStyleProfile(options = {}) {
  if (!state.providers?.profiles?.length) await refreshProviders();
  const recentStyleJob = [...(state.jobs?.jobs || [])].reverse().find(
    (job) => job.kind === "style_profile" &&
      ["queued", "running", "stopping", "detached", "failed", "completed"].includes(job.status));
  const attachedJob = options.jobId
    ? state.jobs?.jobs?.find((job) => job.id === options.jobId)
    : recentStyleJob;
  state.stylePhotoPaths = [...new Set(
    options.photoPaths || attachedJob?.photo_examples || [])];
  state.styleHiddenPaths = new Set(options.hiddenPaths || []);
  state.styleCandidatePaths = new Set(options.candidatePaths || []);
  state.styleExtractionJobId = options.jobId || attachedJob?.id || null;
  $("#style-existing").value = attachedJob?.status === "completed"
    ? attachedJob.output : "";
  const extractionStatus = $("#style-selection-status");
  extractionStatus.textContent = options.jobId
    ? "Highlighted photographs were used by the completed profile extraction."
    : "Add finished photographs to preview them here.";
  renderStylePhotoSelection();
  const profiles = state.providers?.profiles || [];
  const profile = profiles.find((item) => item.kind === "openrouter") || profiles[0];
  $("#style-provider").value = profile?.id || "";
  $("#style-model").value = options.model || "openai/gpt-5.6-luna-pro";
  $("#style-model").disabled = !profile || profile.kind !== "openrouter";
  $("#style-provider-help").textContent = profile
    ? `${providerKindLabel(profile.kind)} route · ${profile.name}`
    : "Configure an OpenRouter profile in Queue → Provider profiles first.";
  const priorName = attachedJob?.status === "completed"
    ? attachedJob.output.split(/[\\/]/).pop() : "";
  const versionMatch = priorName.match(/^(.*)-v(\d+)\.json$/i);
  const nextName = versionMatch
    ? `${versionMatch[1]}-v${Number(versionMatch[2]) + 1}.json`
    : priorName ? `${priorName.replace(/\.json$/i, "")}-v2.json`
      : "personal-style-profile-v2.json";
  const projectFolder = String(state.project?.path || "")
    .replace(/[\\/][^\\/]+$/, "");
  const priorFolder = String(attachedJob?.output || "")
    .replace(/[\\/][^\\/]+$/, "");
  const outputFolder = priorFolder || projectFolder;
  $("#style-output").value = options.output ||
    (outputFolder ? `${outputFolder}/${nextName}` : nextName);
  $("#style-profile-error").replaceChildren();
  renderStyleExtractionProgress();
  $("#style-profile-dialog").showModal();
  if (state.styleExtractionJobId) refreshJobs(true);
}

function currentStyleExtractionJob() {
  return state.jobs?.jobs?.find(
    (job) => job.id === state.styleExtractionJobId) || null;
}

function renderStyleExtractionProgress() {
  const panel = $("#style-extraction-progress");
  if (!panel) return;
  const job = currentStyleExtractionJob();
  panel.hidden = !job;
  if (!job) return;
  const progress = styleProfileProgress(job);
  const fraction = Math.max(0, Math.min(1, Number(progress.fraction) || 0));
  $("#style-extraction-progress-bar").value =
    job.status === "completed" ? 1 : fraction;
  $("#style-extraction-percent").textContent =
    `${job.status === "completed" ? 100 : Math.round(fraction * 100)}%`;
  $("#style-extraction-stage").textContent = job.status === "failed"
    ? "Extraction needs attention" : progress.stage;
  $("#style-extraction-detail").textContent = job.status === "failed"
    ? job.message : `${progress.detail} · ${job.message}`;
  panel.dataset.status = job.status;
  const active = ["queued", "running", "stopping", "detached"].includes(job.status);
  $("#submit-style-profile").disabled = active;
  $("#submit-style-profile").textContent = active
    ? "Extraction in progress…" : job.status === "failed"
      ? "Start extraction again" : "Start another extraction";
  $("#style-use-completed").hidden = job.status !== "completed";
  $("#style-review-examples").hidden = job.status !== "completed";
  if (active) {
    $("#style-profile-summary").textContent =
      `${progress.stage} · ${Math.round(fraction * 100)}%`;
    $("#style-build-profile").textContent = "View extraction";
  } else if (job.status === "failed") {
    $("#style-profile-summary").textContent = "Profile extraction needs attention.";
    $("#style-build-profile").textContent = "Review extraction";
  }
}

async function useCompletedStyleProfile() {
  const job = currentStyleExtractionJob();
  if (!job || job.status !== "completed") return;
  try {
    state.project = await operationalPost("/api/project", {
      active_style_profile: job.output, stage: "style",
    });
    setSaveStatus("Personal style profile selected for this project.", "success");
    renderShortlistWorkspace();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function renderStylePhotoSelection() {
  const input = $("#style-photos");
  const summary = $("#style-photos-summary");
  const status = $("#style-selection-status");
  const list = $("#style-photo-list");
  const visiblePaths = state.stylePhotoPaths.filter(
    (path) => !state.styleHiddenPaths.has(path));
  const count = visiblePaths.length;
  const hiddenCount = state.stylePhotoPaths.length - count;
  input.value = state.stylePhotoPaths.length
    ? `${count} visible · ${state.stylePhotoPaths.length} added`
    : "";
  summary.textContent = state.stylePhotoPaths.length
    ? `${count} visible${hiddenCount ? ` · ${hiddenCount} hidden` : ""} · Add more from another folder if needed.`
    : "You can add files from multiple folders.";
  if (state.styleCandidatePaths.size) {
    status.textContent = `${state.styleCandidatePaths.size} photograph${state.styleCandidatePaths.size === 1 ? "" : "s"} used by the profile extractor. Highlighted cards are the extracted examples.`;
  } else {
    status.textContent = state.stylePhotoPaths.length
      ? "Review the thumbnails, then hide any photograph you do not want included."
      : "Add finished photographs to preview them here.";
  }
  list.replaceChildren();
  for (const [index, path] of state.stylePhotoPaths.entries()) {
    const row = document.createElement("div");
    row.className = "style-photo-item";
    const hidden = state.styleHiddenPaths.has(path);
    const candidate = state.styleCandidatePaths.has(path);
    if (hidden) row.classList.add("is-hidden");
    if (candidate) row.classList.add("is-candidate");
    row.setAttribute("role", "listitem");
    row.setAttribute("aria-selected", String(candidate));
    const image = document.createElement("img");
    image.className = "style-photo-thumb";
    image.alt = path.split(/[\\/]/).pop() || path;
    image.loading = "lazy";
    image.src = stylePhotoUrl(path);
    image.addEventListener("error", () => {
      image.removeAttribute("src");
      image.classList.add("is-unavailable");
      image.textContent = "Preview unavailable";
    });
    const body = document.createElement("div");
    body.className = "style-photo-item-body";
    const copy = document.createElement("div");
    copy.className = "style-photo-item-copy";
    const name = document.createElement("strong");
    name.textContent = path.split(/[\\/]/).pop() || path;
    const location = document.createElement("small");
    location.textContent = path;
    location.title = path;
    copy.append(name, location);
    const meta = document.createElement("div");
    meta.className = "style-photo-item-meta";
    const badge = document.createElement("span");
    badge.className = "style-photo-badge";
    badge.textContent = candidate ? "Used by extractor" : hidden ? "Hidden" : "Included";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "style-photo-remove quiet-button";
    remove.textContent = hidden ? "Show" : "Hide";
    remove.setAttribute("aria-label", `${hidden ? "Show" : "Hide"} ${name.textContent}`);
    remove.addEventListener("click", () => {
      if (hidden) state.styleHiddenPaths.delete(path);
      else state.styleHiddenPaths.add(path);
      renderStylePhotoSelection();
    });
    meta.append(badge, remove);
    body.append(copy, meta);
    row.append(image, body);
    list.append(row);
  }
}

function stylePhotoUrl(path) {
  return `/api/style-photo?path=${encodeURIComponent(path)}`;
}

async function pickStylePhotos() {
  try {
    const result = await operationalPost("/api/jobs/pick-style-photos", {});
    const chosen = Array.isArray(result.photos) ? result.photos : [];
  state.stylePhotoPaths = [...new Set([...state.stylePhotoPaths, ...chosen])];
  for (const path of chosen) state.styleHiddenPaths.delete(path);
  renderStylePhotoSelection();
  } catch (error) {
    if (!/cancel/i.test(error.message)) setSaveStatus(error.message, "error");
  }
}

async function pickStyleExisting() {
  try {
    const result = await operationalPost("/api/jobs/pick-style-profile", {});
    $("#style-existing").value = result.existing || "";
  } catch (error) {
    if (!/cancel/i.test(error.message)) setSaveStatus(error.message, "error");
  }
}

async function pickStyleOutput() {
  try {
    const result = await operationalPost("/api/jobs/pick-style-output", {});
    $("#style-output").value = result.output || "";
  } catch (error) {
    if (!/cancel/i.test(error.message)) setSaveStatus(error.message, "error");
  }
}

function knownStyleProfiles() {
  const paths = [];
  if (state.project?.active_style_profile) {
    paths.push(state.project.active_style_profile);
  }
  for (const job of [...(state.jobs?.jobs || [])].reverse()) {
    if (job.kind === "style_profile" && job.status === "completed" && job.output) {
      paths.push(job.output);
    }
  }
  return [...new Set(paths)];
}

function populateStyleProfileOptions() {
  const profiles = knownStyleProfiles();
  const list = $("#personal-style-profile-options");
  list.replaceChildren();
  for (const path of profiles) {
    const option = document.createElement("option");
    option.value = path;
    option.label = path.split(/[\\/]/).pop() || path;
    list.append(option);
  }
  const input = $("#edit-direction-style-profile");
  if (!input.value && profiles.length) {
    input.value = state.project?.active_style_profile || profiles[0];
  }
  return profiles;
}

function renderPersonalStyleAvailability(profiles = knownStyleProfiles()) {
  const hasProfile = Boolean(
    profiles.length || $("#edit-direction-style-profile").value.trim());
  $("#readiness-style").textContent = hasProfile ? "Selected" : "Not selected";
  $("#personal-style-suggestion").hidden = hasProfile;
  $("#open-style-profile").hidden = hasProfile;
}

async function pickEditStyleProfile() {
  try {
    const result = await operationalPost("/api/jobs/pick-style-profile", {});
    $("#edit-direction-style-profile").value = result.existing || "";
    renderPersonalStyleAvailability();
  } catch (error) {
    if (!/cancel/i.test(error.message)) setSaveStatus(error.message, "error");
  }
}

async function submitStyleProfile() {
  const photos = state.stylePhotoPaths.filter(
    (path) => !state.styleHiddenPaths.has(path));
  if (!photos.length) {
    setSaveStatus("Choose at least one finished photograph", "error");
    return;
  }
  const button = $("#submit-style-profile"); button.disabled = true;
  try {
    const payload = await operationalPost("/api/jobs/add-style-profile", {
      photos, existing: $("#style-existing").value.trim(),
      output: $("#style-output").value.trim(),
      mode: $("#style-mode").value, provider_profile_id: $("#style-provider").value,
      model: $("#style-model").value.trim(),
    });
    state.jobs = payload;
    const job = [...payload.jobs].reverse().find(
      (item) => item.kind === "style_profile" && item.status === "queued");
    state.styleExtractionJobId = job?.id || null;
    setSaveStatus("Personal style extraction started");
    renderStyleExtractionProgress();
    refreshJobs(true);
  } catch (error) {
    const alerts = $("#style-profile-error"); alerts.replaceChildren();
    const message = document.createElement("div"); message.className = "alert fallback";
    message.textContent = error.message; alerts.append(message);
  } finally {
    if (!currentStyleExtractionJob()) button.disabled = false;
  }
}

async function openStyleExtractionExamples(job) {
  try {
    const response = await fetch(
      `/api/jobs/style-profile-result?job_id=${encodeURIComponent(job.id)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not load extracted examples");
    openStyleProfile({
      photoPaths: result.examples || job.photo_examples || [],
      candidatePaths: result.selected_examples || result.examples || [],
      jobId: job.id,
    });
    setSaveStatus("Showing the photographs used by the profile extractor", "success");
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function submitSemanticVerification() {
  const original = `${state.payload.photos_root}/${state.activeShortlistPhoto}`;
  const developed = $("#semantic-developed").value.trim();
  const suggestion = $("#semantic-suggestion").value.trim();
  if (!developed || !suggestion) {
    setSaveStatus("Developed image and edit suggestion are required", "error");
    return;
  }
  const button = $("#submit-semantic-verification");
  button.disabled = true;
  try {
    const payload = await operationalPost("/api/jobs/add-semantic-verification", {
      original, developed,
      thumbnail: $("#semantic-thumbnail").value.trim(), suggestion,
      provider_profile_id: $("#semantic-provider").value,
      model: $("#semantic-model").value.trim(),
      consensus: $("#semantic-consensus").checked,
    });
    $("#semantic-verification-dialog").close();
    state.jobs = payload;
    setSaveStatus("Semantic verification added to Queue");
    refreshJobs();
  } catch (error) {
    const alerts = $("#semantic-verification-error");
    alerts.replaceChildren();
    const message = document.createElement("div");
    message.className = "alert fallback";
    message.textContent = error.message;
    alerts.append(message);
  } finally {
    button.disabled = false;
  }
}

async function loadEditDirections() {
  try {
    const response = await fetch("/api/edit-directions");
    const payload = await response.json();
    if (!response.ok) throw new Error(
      payload.error || "Could not load edit directions");
    state.editDirections = payload.available ? payload.directions : null;
    if (state.shortlist) renderShortlistList();
    if (payload.partial && payload.available) {
      const count = payload.directions?.entries?.length || 0;
      setSaveStatus(
        `${count} existing edit direction${count === 1 ? "" : "s"} available · ${payload.missing_photos?.length || 0} selected photograph${payload.missing_photos?.length === 1 ? "" : "s"} still need generation`,
        "attention");
    }
    if (state.activeShortlistPhoto) {
      renderEditDirections(state.activeShortlistPhoto);
    }
    return payload;
  } catch (error) {
    setSaveStatus(error.message, "error");
    return {available: false};
  }
}

async function loadEditDirectionProviders() {
  const select = $("#edit-direction-provider");
  const model = $("#edit-direction-model");
  try {
    const response = await fetch("/api/providers");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Providers unavailable");
    state.providers = payload;
    select.replaceChildren();
    for (const profile of payload.profiles || []) {
      const option = document.createElement("option");
      option.value = profile.id;
      option.textContent = providerOptionLabel(profile);
      select.append(option);
    }
    const syncModel = () => {
      const profile = payload.profiles?.find(
        (item) => item.id === select.value);
      model.disabled = !profile;
      model.placeholder = profile
        ? `Default: ${profile.models.A}`
        : "Choose a provider profile first";
      if (model.disabled) model.value = "";
    };
    select.onchange = syncModel;
    syncModel();
    $("#generate-edit-directions").disabled = !select.options.length;
  } catch (error) {
    select.replaceChildren();
    $("#generate-edit-directions").disabled = true;
  }
}

async function monitorEditDirectionJob(jobId) {
  try {
    const response = await fetch("/api/jobs");
    const payload = await response.json();
    const job = payload.jobs?.find((item) => item.id === jobId);
    if (!job) throw new Error("Edit-direction job disappeared from Queue");
    setSaveStatus(job.message);
    if (job.status === "completed") {
      state.editDirectionJobId = null;
      await loadEditDirections();
      setSaveStatus("Professional edit directions are ready");
      return;
    }
    if (["failed", "cancelled", "paused"].includes(job.status)) {
      state.editDirectionJobId = null;
      setSaveStatus(job.message, "error");
      return;
    }
    setTimeout(() => monitorEditDirectionJob(jobId), 1200);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function chooseEditDirectionGeneration(current) {
  const dialog = $("#edit-regeneration-dialog");
  const processed = Number(current.processed_count || 0);
  const missing = Number(current.missing_photos?.length || 0);
  $("#edit-regeneration-message").textContent = missing
    ? `${processed} selected photograph${processed === 1 ? " already has" : "s already have"} editing ideas; ${missing} ${missing === 1 ? "is" : "are"} missing.`
    : `All ${processed} selected photograph${processed === 1 ? " already has" : "s already have"} editing ideas.`;
  const missingButton = $("#generate-missing-edit-directions");
  missingButton.hidden = missing === 0;
  return new Promise((resolve) => {
    const cancelButton = $("#cancel-edit-regeneration");
    const closeButton = $("#close-edit-regeneration");
    const allButton = $("#regenerate-all-edit-directions");
    const finish = (choice) => {
      dialog.close();
      cancelButton.removeEventListener("click", cancel);
      closeButton.removeEventListener("click", cancel);
      missingButton.removeEventListener("click", chooseMissing);
      allButton.removeEventListener("click", chooseAll);
      dialog.removeEventListener("cancel", escape);
      resolve(choice);
    };
    const cancel = () => finish("cancel");
    const chooseMissing = () => finish("missing");
    const chooseAll = () => finish("all");
    const escape = (event) => { event.preventDefault(); finish("cancel"); };
    cancelButton.addEventListener("click", cancel);
    closeButton.addEventListener("click", cancel);
    missingButton.addEventListener("click", chooseMissing);
    allButton.addEventListener("click", chooseAll);
    dialog.addEventListener("cancel", escape);
    dialog.showModal();
  });
}

async function generateEditDirections(onlyPhoto = "") {
  if (typeof onlyPhoto !== "string") onlyPhoto = "";
  const selected = onlyPhoto
    ? 1
    : Number(state.shortlistReview?.summary?.interesting || 0);
  if (!selected) {
    setSaveStatus(
      "Select and save at least one photograph for edit-direction ideas",
      "error");
    return;
  }
  try {
    let onlyPhotos = [];
    const current = await loadEditDirections();
    const output = current.default_path || "";
    if (!output) {
      setSaveStatus(
        "Darkimiya could not allocate a versioned edit-direction output", "error");
      return;
    }
    const processed = Number(current.processed_count || 0);
    if (onlyPhoto) {
      const confirmed = window.confirm(
        `Regenerate editing ideas for ${onlyPhoto}?\n\n` +
        "Darkimiya will save a one-photograph revision and preserve every other result."
      );
      if (!confirmed) {
        setSaveStatus("Edit-idea regeneration cancelled");
        return;
      }
    } else if (current.regeneration_required && processed) {
      const choice = await chooseEditDirectionGeneration(current);
      if (choice === "cancel") {
        setSaveStatus("Edit-idea regeneration cancelled");
        return;
      }
      if (choice === "missing") {
        onlyPhotos = [...(current.missing_photos || [])];
      }
    }
    const payload = await operationalPost(
      "/api/jobs/add-edit-suggestions", {
        shortlist: state.shortlistDefaultPath,
        review: state.shortlistReview.path,
        photos: state.payload.photos_root,
        output,
        profile: "professional",
        provider_profile_id: $("#edit-direction-provider").value,
        model: $("#edit-direction-model").value.trim(),
        style_profile: $("#edit-direction-style-profile").value.trim(),
        only_photo: onlyPhoto,
        only_photos: onlyPhotos,
      });
    const job = payload.jobs.at(-1);
    state.editDirectionJobId = job.id;
    setSaveStatus(onlyPhoto
      ? `${onlyPhoto} added to the edit-direction queue`
      : onlyPhotos.length
        ? `${onlyPhotos.length} missing photographs added to the edit-direction queue`
        : `${selected} photographs added to the edit-direction queue`);
    monitorEditDirectionJob(job.id);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function showShortlistEntry(photo) {
  if (!state.shortlistVisible.some((entry) => entry.photo === photo)) return;
  state.activeShortlistPhoto = photo;
  document.querySelectorAll("#shortlist-list .shortlist-item").forEach((item) =>
    item.classList.toggle("active", item.dataset.photo === photo));
  renderShortlistDetail();
  requestAnimationFrame(() => {
    document.querySelector(
      `.shortlist-item[data-photo="${CSS.escape(photo)}"]`)
      ?.scrollIntoView({block: "nearest"});
  });
}

function renderShortlistWorkspace() {
  if (!state.shortlist) {
    $("#start-shortlist-assessment").textContent = "Start assessment";
    $("#start-shortlist-assessment").className = "primary-button";
    $("#shortlist-empty").hidden = false;
    $("#shortlist-detail").hidden = true;
    $("#shortlist-list").replaceChildren();
    return;
  }
  const statistics = state.shortlist.statistics || {};
  $("#shortlist-summary").textContent =
    `${state.shortlist.entries.length} assessed · ${
      statistics.raw_available || 0} with RAW · ${
      state.shortlistReview?.summary?.reviewed || 0} human reviewed`;
  $("#edit-direction-selection-count").textContent =
    `${state.shortlistReview?.summary?.interesting || 0} selected for edit ideas`;
  const selected = Number(state.shortlistReview?.summary?.interesting || 0);
  const raw = Number(statistics.raw_available || 0);
  $("#readiness-selected").textContent = String(selected);
  $("#readiness-raw").textContent = `${raw} / ${state.shortlist.entries.length}`;
  $("#start-shortlist-assessment").textContent = "Run assessment again…";
  $("#start-shortlist-assessment").className = "quiet-button";
  const profiles = populateStyleProfileOptions();
  renderPersonalStyleAvailability(profiles);
  renderShortlistList();
  if (!state.shortlistVisible.some(
    (entry) => entry.photo === state.activeShortlistPhoto
  )) {
    state.activeShortlistPhoto = state.shortlistVisible[0]?.photo || null;
  }
  if (!state.shortlistVisible.length) {
    $("#shortlist-empty").hidden = false;
    $("#shortlist-empty-message").textContent =
      "No photographs match the current professional-shortlist filters.";
    $("#shortlist-detail").hidden = true;
  } else {
    renderShortlistDetail();
  }
}

async function loadProfessionalShortlist() {
  try {
    const response = await fetch("/api/shortlist");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load shortlist");
    if (!payload.available) {
      state.shortlist = null;
      state.shortlistReview = null;
      state.shortlistDefaultPath = payload.default_path || "";
      $("#shortlist-nav-count").hidden = true;
      $("#shortlist-summary").textContent = "No shortlist artifact is loaded.";
      $("#shortlist-empty-message").textContent =
        `Expected location: ${payload.default_path}`;
      return;
    }
    state.shortlist = payload.shortlist;
    state.shortlistReview = payload.review;
    state.shortlistDefaultPath = payload.shortlist_path;
    await loadEditDirections();
    await loadEditDirectionProviders();
    const count = $("#shortlist-nav-count");
    count.textContent = String(payload.shortlist.entries.length);
    count.hidden = false;
    state.activeShortlistPhoto ||= payload.shortlist.entries[0]?.photo || null;
    renderShortlistWorkspace();
  } catch (error) {
    $("#shortlist-summary").textContent = error.message;
    $("#shortlist-empty-message").textContent = error.message;
  }
}

function navigateShortlist(delta) {
  const index = state.shortlistVisible.findIndex(
    (entry) => entry.photo === state.activeShortlistPhoto);
  const target = state.shortlistVisible[index + delta];
  if (target) showShortlistEntry(target.photo);
}

async function saveShortlistReview(advance = false) {
  const entry = activeShortlistEntry();
  if (!entry || !state.shortlistReview) return;
  const previousVisible = state.shortlistVisible.map((item) => item.photo);
  const previousIndex = previousVisible.indexOf(entry.photo);
  try {
    const payload = await operationalPost("/api/shortlist/review", {
      photo: entry.photo,
      tier: $("#shortlist-human-tier").value,
      edit_raw: $("#shortlist-edit-raw").checked,
      interesting: $("#shortlist-interesting").checked,
      reviewed: $("#shortlist-reviewed").checked,
      note: $("#shortlist-note").value,
      revision: state.shortlistReview.revision,
    });
    state.shortlistReview = payload;
    setSaveStatus("Professional shortlist decision saved");
    const nextVisible = filteredShortlistEntries();
    const membershipChanged = previousVisible.length !== nextVisible.length
      || previousVisible.some((photo, index) => photo !== nextVisible[index]?.photo);
    state.shortlistVisible = nextVisible;
    if (membershipChanged) {
      renderShortlistList();
    } else {
      refreshShortlistListItem(entry.photo);
    }
    const nextPhoto = advance
      ? (membershipChanged
          ? nextVisible[Math.min(previousIndex, nextVisible.length - 1)]?.photo
          : nextVisible[previousIndex + 1]?.photo)
      : entry.photo;
    if (nextPhoto && nextVisible.some((item) => item.photo === nextPhoto)) {
      showShortlistEntry(nextPhoto);
    } else if (nextVisible.length) {
      showShortlistEntry(nextVisible.at(-1).photo);
    } else {
      renderShortlistWorkspace();
    }
    $("#shortlist-summary").textContent =
      `${state.shortlist.entries.length} assessed · ${
        state.shortlist.statistics?.raw_available || 0} with RAW · ${
        state.shortlistReview?.summary?.reviewed || 0} human reviewed`;
    $("#edit-direction-selection-count").textContent =
      `${state.shortlistReview?.summary?.interesting || 0} selected for edit ideas`;
    $("#readiness-selected").textContent = String(
      state.shortlistReview?.summary?.interesting || 0);
  } catch (error) {
    setSaveStatus(error.message, "error");
    if (error.message.toLowerCase().includes("reload")) {
      await loadProfessionalShortlist();
    }
  }
}

async function undoShortlistReview() {
  if (!state.shortlistReview) return;
  try {
    state.shortlistReview = await operationalPost("/api/shortlist/undo", {
      revision: state.shortlistReview.revision,
    });
    setSaveStatus("Professional shortlist change undone");
    renderShortlistWorkspace();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function professionalJobForCurrentReport(jobs) {
  const reportPath = state.payload?.report_path;
  return [...(jobs || [])].reverse().find(
    (job) => job.kind === "professional_shortlist"
      && job.report === reportPath) || null;
}

function renderShortlistJob(job) {
  const panel = $("#shortlist-job-status");
  if (!job || job.status === "completed") {
    panel.hidden = true;
    $("#start-shortlist-assessment").disabled = false;
    return;
  }
  panel.hidden = false;
  const presentation = jobStatusPresentation[job.status] || {
    label: job.status, stage: job.message || "", tone: "",
  };
  $("#shortlist-job-title").textContent =
    `Assessment · ${presentation.label}`;
  $("#shortlist-job-message").textContent =
    job.message || presentation.stage;
  const progress = job.progress || {};
  const total = Number(progress.total_items || progress.total_clusters || 0);
  const completed = Number(
    progress.completed_items || progress.completed_clusters || 0);
  $("#shortlist-job-progress").max = Math.max(1, total);
  $("#shortlist-job-progress").value = completed;
  $("#shortlist-job-progress-text").textContent = total
    ? `${completed} of ${total} photographs assessed · ${
      Math.round(completed / total * 100)}%`
    : job.status === "queued"
      ? "Preparing selected photographs…"
      : "Discovering assessment candidates…";
  $("#start-shortlist-assessment").disabled =
    ["queued", "running", "stopping", "detached"].includes(job.status);
}

async function monitorShortlistJob() {
  clearTimeout(state.shortlistJobPoll);
  try {
    const response = await fetch("/api/jobs");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Queue is unavailable");
    const job = professionalJobForCurrentReport(payload.jobs);
    renderShortlistJob(job);
    if (!job) return;
    if (job.status === "completed" && state.shortlistLoadedJobId !== job.id) {
      const loaded = await operationalPost("/api/shortlist/load", {
        path: job.output,
      });
      state.shortlist = loaded.shortlist;
      state.shortlistReview = loaded.review;
      state.shortlistLoadedJobId = job.id;
      state.activeShortlistPhoto =
        loaded.shortlist.entries[0]?.photo || null;
      const count = $("#shortlist-nav-count");
      count.textContent = String(loaded.shortlist.entries.length);
      count.hidden = false;
      renderShortlistWorkspace();
      setSaveStatus("Professional shortlist is ready");
    }
    if (["queued", "running", "stopping", "detached"].includes(job.status)) {
      state.shortlistJobPoll = setTimeout(monitorShortlistJob, 1000);
    }
  } catch (error) {
    renderShortlistJob(null);
    $("#start-shortlist-assessment").disabled = true;
    $("#start-shortlist-assessment").title = error.message;
  }
}

function providerTrustText(profile) {
  if (!profile) return "Choose a configured provider profile.";
  if (profile.privacy === "local") {
    return "Local provider: observed previews remain on this Mac or your configured local endpoint.";
  }
  if (profile.privacy === "remote-zdr") {
    return "Remote Zero Data Retention profile: generated previews leave this Mac for the configured vision models.";
  }
  return "Remote provider: review its retention, privacy, and cost policy before starting.";
}

async function openShortlistGenerator() {
  const dialog = $("#shortlist-generate-dialog");
  $("#shortlist-source-report").textContent = state.payload.report_path;
  $("#shortlist-source-photos").textContent = state.payload.photos_root;
  const savedReviewPath = Number(state.review?.revision || 0) > 0
    ? state.review.path : "";
  $("#shortlist-source-review").textContent =
    savedReviewPath || "No saved human review; AI selections will be used";
  const defaultOutput = state.shortlistDefaultPath || `${state.payload.report_path
    .replace(/\.json$/i, "")}.professional-shortlist.json`;
  const timestamp = new Date().toISOString()
    .replace(/[-:]/g, "").replace(/\..+$/, "").replace("T", "-");
  $("#shortlist-generate-output").value = state.shortlist
    ? defaultOutput.replace(
      /\.json$/i, `.reassessment-${timestamp}.json`)
    : defaultOutput;
  $("#shortlist-generate-error").replaceChildren();
  const select = $("#shortlist-generate-provider");
  select.replaceChildren();
  $("#submit-shortlist-generate").disabled = true;
  try {
    const response = await fetch("/api/providers");
    const payload = await response.json();
    if (!response.ok) throw new Error(
      payload.error || "Provider profiles are unavailable");
    state.providers = payload;
    for (const profile of payload.profiles || []) {
      const option = document.createElement("option");
      option.value = profile.id;
      option.textContent = `${profile.name} · ${profile.kind}`;
      select.append(option);
    }
    if (!select.options.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Configure a provider in Queue first";
      select.append(option);
      throw new Error(
        "No AI provider profile exists. Open Queue → Provider profiles and configure OpenRouter, Ollama, or another endpoint.");
    }
    $("#submit-shortlist-generate").disabled = false;
    $("#shortlist-generate-trust").textContent =
      providerTrustText(payload.profiles.find(
        (profile) => profile.id === select.value));
  } catch (error) {
    const message = document.createElement("div");
    message.className = "alert fallback";
    message.textContent = error.message;
    $("#shortlist-generate-error").append(message);
  }
  dialog.showModal();
}

async function submitShortlistAssessment() {
  const button = $("#submit-shortlist-generate");
  try {
    button.disabled = true;
    button.textContent = "Adding to Queue…";
    const payload = await operationalPost("/api/jobs/add-professional", {
      report: state.payload.report_path,
      photos: state.payload.photos_root,
      review: Number(state.review?.revision || 0) > 0
        ? state.review.path : "",
      output: $("#shortlist-generate-output").value,
      policy: $("#shortlist-generate-policy").value,
      profile: $("#shortlist-generate-profile").value,
      provider_profile_id: $("#shortlist-generate-provider").value,
    });
    $("#shortlist-generate-dialog").close();
    renderShortlistJob(professionalJobForCurrentReport(payload.jobs));
    setSaveStatus("Professional assessment added to Queue");
    monitorShortlistJob();
  } catch (error) {
    const alerts = $("#shortlist-generate-error");
    alerts.replaceChildren();
    const message = document.createElement("div");
    message.className = "alert fallback";
    message.textContent = error.message;
    alerts.append(message);
  } finally {
    button.disabled = false;
    button.textContent = "Add to Queue";
  }
}

function bindEvents() {
  document.querySelectorAll(".project-stage").forEach((button) =>
    button.addEventListener("click", () => setProjectStage(button.dataset.stage)));
  $("#inspector-toggle").addEventListener(
    "click", () => setInspectorOpen(!state.inspectorOpen));
  $("#close-review-inspector").addEventListener(
    "click", () => setInspectorOpen(false));
  const inspectorTabs = [...document.querySelectorAll(".inspector-tab")];
  inspectorTabs.forEach((button) => {
    button.addEventListener("click", () => setInspectorTab(button.dataset.inspectorTab));
    button.addEventListener("keydown", (event) => {
      const current = inspectorTabs.indexOf(button);
      let target = current;
      if (event.key === "ArrowRight") target = (current + 1) % inspectorTabs.length;
      else if (event.key === "ArrowLeft") {
        target = (current - 1 + inspectorTabs.length) % inspectorTabs.length;
      } else if (event.key === "Home") target = 0;
      else if (event.key === "End") target = inspectorTabs.length - 1;
      else return;
      event.preventDefault();
      setInspectorTab(inspectorTabs[target].dataset.inspectorTab);
      inspectorTabs[target].focus();
    });
  });
  $("#inspector-toggle-keeper").addEventListener("click", () => {
    const cluster = state.clusters.find((item) => item.cluster_id === state.activeClusterId);
    if (cluster && state.activeReviewPhoto) toggleHumanKeeper(cluster, state.activeReviewPhoto);
  });
  $("#inspector-compare-photo").addEventListener("click", () => {
    if (state.activeReviewPhoto) toggleCompare(state.activeReviewPhoto);
  });
  $("#inspector-open-photo").addEventListener("click", () => {
    if (state.activeReviewPhoto) openViewer([state.activeReviewPhoto]);
  });
  for (const selector of [
    "#inspector-photo-flag", "#inspector-photo-rating", "#inspector-photo-label",
  ]) {
    $(selector).addEventListener("change", () => {
      const cluster = state.clusters.find((item) => item.cluster_id === state.activeClusterId);
      if (!cluster || !state.activeReviewPhoto) return;
      savePhotoAnnotation(cluster, state.activeReviewPhoto, {
        flag: $("#inspector-photo-flag").value,
        rating: $("#inspector-photo-rating").value,
        label: $("#inspector-photo-label").value,
      });
    });
  }
  $("#inspector-open-recovery").addEventListener("click", () => openRecoveryCenter());
  $("#inspector-link-raw").addEventListener("click", pickRawSource);
  $("#raw-source-button").addEventListener("click", pickRawSource);
  $("#generate-edit-directions").addEventListener(
    "click", generateEditDirections);
  $("#regenerate-edit-direction").addEventListener(
    "click", (event) => generateEditDirections(event.currentTarget.dataset.photo));
  $("#open-semantic-verification").addEventListener(
    "click", openSemanticVerification);
  $("#close-semantic-verification").addEventListener(
    "click", () => $("#semantic-verification-dialog").close());
  $("#cancel-semantic-verification").addEventListener(
    "click", () => $("#semantic-verification-dialog").close());
  $("#submit-semantic-verification").addEventListener(
    "click", submitSemanticVerification);
  $("#open-style-profile").addEventListener("click", openStyleProfile);
  $("#close-style-profile").addEventListener(
    "click", () => $("#style-profile-dialog").close());
  $("#cancel-style-profile").addEventListener(
    "click", () => $("#style-profile-dialog").close());
  $("#pick-style-photos").addEventListener("click", pickStylePhotos);
  $("#pick-style-existing").addEventListener("click", pickStyleExisting);
  $("#pick-style-output").addEventListener("click", pickStyleOutput);
  $("#pick-edit-style-profile").addEventListener(
    "click", pickEditStyleProfile);
  $("#edit-direction-style-profile").addEventListener(
    "input", () => renderPersonalStyleAvailability());
  $("#submit-style-profile").addEventListener("click", submitStyleProfile);
  $("#style-use-completed").addEventListener("click", useCompletedStyleProfile);
  $("#style-review-examples").addEventListener("click", () => {
    const job = currentStyleExtractionJob();
    if (job) openStyleExtractionExamples(job);
  });
  $("#style-back-to-shortlist").addEventListener(
    "click", () => setProjectStage("shortlist"));
  $("#style-build-profile").addEventListener("click", openStyleProfile);
  $("#style-empty-build").addEventListener("click", openStyleProfile);
  $("#develop-back-to-style").addEventListener("click", () => setProjectStage("style"));
  $("#develop-open-verify").addEventListener("click", () => setProjectStage("verify"));
  $("#develop-render-button").addEventListener("click", queueDevelopmentRender);
  $("#develop-guided-render").addEventListener("click", renderSelectedDevelopment);
  $("#develop-export-active").addEventListener("click", exportActiveDevelopment);
  $("#develop-reveal-export").addEventListener("click", async () => {
    if (!state.lastDevelopmentExportPath) return;
    try {
      await operationalPost(
        "/api/export/reveal", {path: state.lastDevelopmentExportPath});
    } catch (error) {
      showDevelopmentExportStatus("error", "Could not open Finder", error.message);
    }
  });
  $("#develop-import-recipe").addEventListener("click", importDevelopmentRecipe);
  document.querySelectorAll(".develop-ab-button").forEach((button) =>
    button.addEventListener("click", () =>
      setDevelopmentCompareMode(button.dataset.developView)));
  $("#develop-advanced-edit").addEventListener("click", () => {
    const panel = $("#develop-advanced-panel");
    panel.hidden = !panel.hidden;
    $("#develop-advanced-edit").textContent = panel.hidden
      ? "Advanced editing" : "Hide advanced editing";
  });
  $("#develop-engine").addEventListener(
    "change", saveDevelopmentRenderingPreference);
  $("#develop-demosaic").addEventListener(
    "change", saveDevelopmentRenderingPreference);
  $("#develop-compare-renderers").addEventListener("click", queueRendererComparison);
  $("#develop-render-full-darktable").addEventListener(
    "click", queueFullResolutionDarktable);
  $("#verify-back-to-develop").addEventListener("click", () => setProjectStage("develop"));
  $("#verify-adjust-button").addEventListener("click", openAdjustmentDialog);
  $("#close-adjustment").addEventListener("click", () => $("#adjustment-dialog").close());
  $("#cancel-adjustment").addEventListener("click", () => $("#adjustment-dialog").close());
  $("#save-adjustment").addEventListener("click", saveAdjustmentRevision);
  $("#develop-search").addEventListener("input", renderDevelopmentPhotoList);
  $("#start-shortlist-assessment").addEventListener(
    "click", openShortlistGenerator);
  $("#empty-start-shortlist-assessment").addEventListener(
    "click", openShortlistGenerator);
  $("#close-shortlist-generate").addEventListener(
    "click", () => $("#shortlist-generate-dialog").close());
  $("#cancel-shortlist-generate").addEventListener(
    "click", () => $("#shortlist-generate-dialog").close());
  $("#submit-shortlist-generate").addEventListener(
    "click", submitShortlistAssessment);
  $("#shortlist-generate-provider").addEventListener("change", (event) => {
    const profile = state.providers?.profiles?.find(
      (item) => item.id === event.target.value);
    $("#shortlist-generate-trust").textContent = providerTrustText(profile);
  });
  $("#shortlist-search").addEventListener("input", (event) => {
    state.shortlistQuery = event.target.value.trim();
    renderShortlistWorkspace();
  });
  for (const id of [
    "#shortlist-tier-filter", "#shortlist-raw-filter",
    "#shortlist-review-filter",
  ]) {
    $(id).addEventListener("change", renderShortlistWorkspace);
  }
  $("#previous-shortlist").addEventListener(
    "click", () => navigateShortlist(-1));
  $("#next-shortlist").addEventListener(
    "click", () => navigateShortlist(1));
  $("#shortlist-open-viewer").addEventListener("click", () => {
    const entry = activeShortlistEntry();
    if (entry) openViewer([entry.photo], [entry.photo]);
  });
  $("#open-origin-cluster").addEventListener("click", () => {
    const entry = activeShortlistEntry();
    if (!entry) return;
    switchWorkspace("review");
    showCluster(entry.cluster_id);
  });
  $("#save-shortlist-review").addEventListener(
    "click", () => saveShortlistReview(false));
  $("#save-shortlist-next").addEventListener(
    "click", () => saveShortlistReview(true));
  $("#undo-shortlist-review").addEventListener(
    "click", undoShortlistReview);
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    state.clusterWindowStart = 0;
    renderClusterList();
    updateNavigation();
  });
  $("#filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    state.activeFilter = button.dataset.filter;
    state.clusterWindowStart = 0;
    document.querySelectorAll(".filter").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    $("#cluster-filter-menu").open = false;
    renderClusterList();
    updateNavigation();
  });
  document.addEventListener("click", (event) => {
    const menu = $("#cluster-filter-menu");
    if (menu.open && !menu.contains(event.target)) menu.open = false;
    const reviewMenu = $("#review-more-actions");
    if (reviewMenu.open && !reviewMenu.contains(event.target)) reviewMenu.open = false;
  });
  $("#previous-cluster-window").addEventListener(
    "click", () => moveClusterWindow(-1));
  $("#next-cluster-window").addEventListener(
    "click", () => moveClusterWindow(1));
  $("#cluster-list").addEventListener("keydown", (event) => {
    const items = [...document.querySelectorAll("#cluster-list .cluster-item")];
    const index = items.indexOf(document.activeElement);
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextIndex = event.key === "Home" ? 0 : event.key === "End"
      ? items.length - 1 : Math.max(
        0, Math.min(items.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
    items[nextIndex]?.focus();
  });
  $("#previous-cluster").addEventListener("click", () => navigate(-1));
  $("#next-cluster").addEventListener("click", () => navigate(1));
  $("#accept-ai-next").addEventListener("click", acceptSelectionAndNext);
  $("#accept-ai").addEventListener("click", () => acceptAIRecommendation());
  $("#keep-none").addEventListener("click", () => updateActiveReview(
    (draft) => ({...draft, keepers: [], reviewed: true}), "No keepers selected"));
  $("#mark-reviewed").addEventListener("click", () => updateActiveReview(
    (draft) => ({...draft, reviewed: true}), "Cluster marked reviewed"));
  $("#reset-review").addEventListener("click", () => updateActiveReview((draft) => {
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    return {...draft, keepers: [...decisionFor(cluster).photos], reviewed: false};
  }, "Cluster returned to unreviewed"));
  $("#skip-group").addEventListener("click", skipActiveGroup);
  $("#actionbar-undo").addEventListener("click", () => $("#undo-review").click());
  $("#review-more-actions").addEventListener("click", (event) => {
    if (event.target.closest("button")) $("#review-more-actions").open = false;
  });
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
  $("#open-first-cluster").addEventListener("click", () => {
    const first = state.visible[0] || state.clusters[0];
    if (first) showCluster(first.cluster_id);
  });
  $("#recovery-button").addEventListener("click", () => openRecoveryCenter());
  $("#shortcuts-button").addEventListener("click", toggleShortcuts);
  $("#close-shortcuts").addEventListener("click", toggleShortcuts);
  $("#dismiss-shortcuts").addEventListener("click", toggleShortcuts);
  $("#open-recovery-from-banner").addEventListener(
    "click", () => openRecoveryCenter());
  $("#close-recovery").addEventListener(
    "click", () => $("#recovery-dialog").close());
  $("#dismiss-recovery").addEventListener(
    "click", () => $("#recovery-dialog").close());
  $("#retry-current-review").addEventListener(
    "click", () => window.location.reload());
  $("#migrate-project").addEventListener("click", migrateLegacyProject);
  $("#recovery-open-queue").addEventListener("click", () => {
    $("#recovery-dialog").close();
    $("#jobs-dialog").showModal();
    refreshProviders();
    refreshJobs(true);
  });
  $("#recovery-open-providers").addEventListener("click", () => {
    $("#recovery-dialog").close();
    $("#providers-dialog").showModal();
    refreshProviders();
  });
  $("#jobs-button").addEventListener("click", () => {
    $("#jobs-dialog").showModal();
    refreshProviders();
    refreshJobs(true);
  });
  $("#people-button").addEventListener("click", () => {
    $("#people-dialog").showModal();
    refreshPeople(true);
  });
  $("#close-people").addEventListener("click", () => {
    $("#people-dialog").close();
    clearTimeout(state.personPoll);
  });
  $("#start-people-index").addEventListener("click", startPeopleIndex);
  $("#pause-people-index").addEventListener(
    "click", () => peopleAction("/api/people/cancel"));
  $("#save-person").addEventListener("click", savePerson);
  $("#merge-people").addEventListener("click", mergePeople);
  $("#people-search").addEventListener("input", (event) => {
    state.peopleQuery = event.target.value.trim();
    renderPeople();
  });
  document.querySelectorAll(".people-filter").forEach((button) => {
    button.addEventListener("click", () => {
      state.peopleFilter = button.dataset.peopleFilter || "all";
      renderPeople();
    });
  });
  $("#split-person").addEventListener("click", splitPerson);
  $("#load-more-faces").addEventListener("click", loadMoreFaces);
  $("#forget-person").addEventListener("click", forgetPerson);
  $("#delete-all-people").addEventListener("click", deleteAllPeople);
  $("#person-filter").addEventListener("change", () => {
    state.clusterWindowStart = 0;
    renderClusterList();
    updateNavigation();
  });
  $("#close-jobs").addEventListener("click", () => {
    $("#jobs-dialog").close();
    clearTimeout(state.jobPoll);
  });
  $("#refresh-jobs").addEventListener("click", () => refreshJobs(false));
  $("#pick-job-folder").addEventListener("click", pickJobFolder);
  $("#add-job").addEventListener("click", addJob);
  $("#job-photos").addEventListener("input", updateJobReadiness);
  $("#job-keepers").addEventListener("input", updateJobReadiness);
  $("#job-provider").addEventListener("change", updateJobReadiness);
  $("#job-judge-votes").addEventListener("input", updateJobReadiness);
  $("#job-judge-required").addEventListener("input", updateJobReadiness);
  document.querySelectorAll(".job-judge-agent").forEach((input) =>
    input.addEventListener("change", updateJobReadiness));
  $("#provider-settings").addEventListener("click", () => {
    $("#providers-dialog").showModal();
    refreshProviders().then(() => editProvider(null));
  });
  $("#close-providers").addEventListener(
    "click", () => $("#providers-dialog").close());
  $("#new-provider").addEventListener("click", () => editProvider(null));
  $("#provider-kind").addEventListener(
    "change", () => updateProviderKind(true));
  $("#provider-name").addEventListener("input", () => renderProviderTrust());
  $("#provider-endpoint").addEventListener("input", () => renderProviderTrust());
  $("#provider-zdr").addEventListener("change", () => renderProviderTrust());
  $("#provider-credential-required").addEventListener(
    "change", () => renderProviderTrust());
  $("#provider-secret").addEventListener("input", () => renderProviderTrust());
  $("#save-provider").addEventListener("click", saveProvider);
  $("#test-provider").addEventListener("click", testProvider);
  $("#delete-provider").addEventListener("click", deleteProvider);
  $("#undo-review").addEventListener("click", undoReview);
  $("#organize-button").addEventListener(
    "click", () => {
      renderSelectionPolicy();
      renderOperationRisk();
      $("#organize").showModal();
      refreshRecoverableOperations(false);
    });
  $("#close-organize").addEventListener(
    "click", () => $("#organize").close());
  document.querySelectorAll(".download-format").forEach((button) => {
    button.addEventListener(
      "click", () => downloadSelection(button.dataset.format));
  });
  $("#export-policy").addEventListener("change", renderSelectionPolicy);
  $("#operation-action").addEventListener("change", renderOperationRisk);
  $("#cleanup-policy").addEventListener("change", renderOperationRisk);
  $("#cleanup-select-all").addEventListener("click", () => {
    document.querySelectorAll("#cleanup-candidate-list input").forEach(
      (input) => { input.checked = true; });
    state.operationPlan = null;
    $("#preflight-result").hidden = true;
  });
  $("#cleanup-select-none").addEventListener("click", () => {
    document.querySelectorAll("#cleanup-candidate-list input").forEach(
      (input) => { input.checked = false; });
    state.operationPlan = null;
    $("#preflight-result").hidden = true;
  });
  $("#operation-scope").addEventListener("change", renderOperationRisk);
  $("#pick-operation-destination").addEventListener(
    "click", pickOperationDestination);
  $("#download-xmp").addEventListener("click", downloadXmp);
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
        previewImageLoads.set(image, Symbol("preview-cache-cleared"));
        delete image.dataset.previewRevision;
        image.classList.add("preview-loading");
        image.removeAttribute("src");
      });
      scheduleVisiblePreviewFocus();
    } catch (error) {
      $("#preview-status").textContent = error.message;
    }
  });
  document.querySelectorAll(".density-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.viewDensity = button.dataset.density === "compact"
        ? "compact" : "comfortable";
      const cluster = state.clusters.find(
        (item) => item.cluster_id === state.activeClusterId);
      if (cluster) renderReviewWorkspace(cluster, decisionFor(cluster));
    });
  });
  $("#open-comparison").addEventListener("click", compareMarked);
  $("#clear-compare").addEventListener("click", () => {
    state.compare = [];
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    if (cluster) showCluster(cluster.cluster_id);
  });
  $("#close-viewer").addEventListener("click", () => $("#viewer").close());
  $("#viewer-zoom").addEventListener("input", (event) => {
    state.viewer.zoom = Number(event.target.value);
    applyViewerTransform();
  });
  $("#viewer-crop").addEventListener("change", (event) => {
    state.viewer.crop = event.target.value;
    state.viewer.x = 0;
    state.viewer.y = 0;
    applyViewerTransform();
  });
  $("#viewer-sync").addEventListener("change", applyViewerTransform);
  $("#viewer-reset").addEventListener("click", resetViewerTransform);
  $("#viewer").addEventListener("click", (event) => {
    if (event.target === $("#viewer")) $("#viewer").close();
  });
  $("#about-button").addEventListener("click", () => $("#about").showModal());
  $("#close-about").addEventListener("click", () => $("#about").close());
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea, select, [contenteditable=true]")) return;
    if (event.key === "?" && $("#shortcuts-dialog").open) {
      event.preventDefault();
      toggleShortcuts();
      return;
    }
    if ($("#viewer").open) {
      if (event.key === "Escape") $("#viewer").close();
      if (event.key === "+" || event.key === "=") {
        state.viewer.zoom = Math.min(5, state.viewer.zoom + 0.2);
        $("#viewer-zoom").value = String(state.viewer.zoom);
        applyViewerTransform();
      }
      if (event.key === "-") {
        state.viewer.zoom = Math.max(1, state.viewer.zoom - 0.2);
        $("#viewer-zoom").value = String(state.viewer.zoom);
        applyViewerTransform();
      }
      return;
    }
    if (
      $("#about").open || $("#organize").open || $("#jobs-dialog").open
      || $("#providers-dialog").open || $("#people-dialog").open
      || $("#recovery-dialog").open || $("#shortcuts-dialog").open
    ) return;
    if (event.key === "?") {
      event.preventDefault();
      toggleShortcuts();
      return;
    }
    if (event.key.toLowerCase() === "i" && state.workspaceMode === "review") {
      event.preventDefault();
      setInspectorOpen(!state.inspectorOpen);
      return;
    }
    if (state.workspaceMode === "professional") {
      if (event.key === "ArrowUp") {
        event.preventDefault();
        navigateShortlist(-1);
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        navigateShortlist(1);
        return;
      }
      if (event.key.toLowerCase() === "e") {
        event.preventDefault();
        $("#shortlist-edit-raw").checked =
          !$("#shortlist-edit-raw").checked;
        return;
      }
      const tiers = [
        "exceptional", "strong", "promising", "ordinary", "reject"];
      const tierIndex = Number(event.key) - 1;
      if (tierIndex >= 0 && tierIndex < tiers.length) {
        event.preventDefault();
        $("#shortlist-human-tier").value = tiers[tierIndex];
        return;
      }
      if (event.key.toLowerCase() === "s") {
        event.preventDefault();
        void saveShortlistReview(false);
        return;
      }
      if (event.key.toLowerCase() === "z") {
        event.preventDefault();
        void undoShortlistReview();
      }
      return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      navigate(-1);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      navigate(1);
      return;
    }
    const cluster = state.clusters.find(
      (item) => item.cluster_id === state.activeClusterId);
    if (!cluster) return;
    if (event.key.toLowerCase() === "c") {
      event.preventDefault();
      compareMarked();
    }
    if (event.key.toLowerCase() === "x" && state.compare.length) {
      event.preventDefault();
      state.compare = [];
      showCluster(cluster.cluster_id);
      setSaveStatus("Comparison cleared");
    }
    if (!state.review.status.compatible) return;
    if (event.key === "ArrowRight" && !event.repeat) {
      event.preventDefault();
      $("#accept-ai").click();
      return;
    }
    const number = Number(event.key);
    if (Number.isInteger(number) && number >= 1 && number <= 9 &&
        cluster.photos[number - 1] && !event.repeat) {
      event.preventDefault();
      toggleHumanKeeper(cluster, cluster.photos[number - 1]);
      return;
    }
    if (event.key.toLowerCase() === "a" && !event.repeat) {
      event.preventDefault();
      void acceptSelectionAndNext();
      return;
    }
    if (event.key.toLowerCase() === "n") {
      event.preventDefault();
      $("#keep-none").click();
    }
    if (event.key.toLowerCase() === "u") {
      event.preventDefault();
      $("#reset-review").click();
    }
    if (event.key.toLowerCase() === "z") {
      event.preventDefault();
      $("#undo-review").click();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.operationId) {
      monitorOperation(state.operationId);
    }
    if (!document.hidden && state.activeClusterId) {
      const cluster = state.clusters.find(
        (item) => item.cluster_id === state.activeClusterId);
      if (cluster) focusPreviews(cluster.photos, [], "thumb");
    }
  });
}

async function initialize() {
  bindEvents();
  try {
    const response = await fetch("/api/report");
    if (!response.ok) throw new Error(`Report request failed (${response.status})`);
    state.payload = await response.json();
    state.project = state.payload.project || null;
    if (state.project) {
      $("#project-name").textContent = state.project.name;
      $("#sidebar-project-name").textContent = state.project.name;
      const stage = state.project.stage || "cull";
      document.querySelectorAll(".project-stage").forEach((button) =>
        button.classList.toggle("active", button.dataset.stage === stage));
      $("#project-stage-note").textContent = stage === "style"
        ? "Personal style" : `${stage[0].toUpperCase()}${stage.slice(1)}`;
    }
    state.rawSource = state.payload.raw_source;
    state.review = state.payload.review;
    state.csrfToken = state.payload.csrf_token;
    state.clusters = state.payload.report.clusters;
    state.decisions = new Map(
      state.payload.report.keep.map((item) => [item.cluster_id, item]),
    );
    await loadProfessionalShortlist();
    monitorShortlistJob();
    renderSummary();
    renderClusterList();
    renderDetails();
    renderRawSource();
    if (!state.review.status.compatible) {
      setSaveStatus(state.review.status.stale_reason, "error");
      setRecoveryBanner(
        "Human review does not match this report",
        "Darkimiya has kept the sidecar read-only. Review recovery options before continuing.",
        "failed");
    }
    if (state.payload.summary.missing_photos) {
      setRecoveryBanner(
        "Some source photographs are unavailable",
        "Reconnect the original photo volume, then retry the current review.",
        "attention");
    }
    const resumeId = state.review.last_cluster_id;
    const initial = state.clusters.some((item) => item.cluster_id === resumeId)
      ? resumeId : state.clusters[0]?.cluster_id;
    if (initial) showCluster(initial);
    const restoredStage = state.project?.stage || "cull";
    switchWorkspace(restoredStage === "shortlist" ? "professional" : restoredStage);
    refreshRecoverableOperations(true);
    refreshPeople(false);
  } catch (error) {
    $("#report-status").textContent = "Could not load report";
    $("#empty-state").innerHTML = "";
    const heading = document.createElement("h2");
    heading.textContent = "Report loading failed";
    const message = document.createElement("p");
    message.textContent = error.message;
    const recover = document.createElement("button");
    recover.type = "button";
    recover.className = "primary-button";
    recover.textContent = "Open recovery guidance";
    recover.addEventListener("click", () => openRecoveryCenter(error.message));
    $("#empty-state").append(heading, message, recover);
    setRecoveryBanner(
      "Report loading failed",
      "The report was not changed. Open recovery guidance for the next safe step.",
      "failed");
  }
}

initialize();
