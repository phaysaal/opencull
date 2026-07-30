"use strict";

const state = {
  payload: null,
  clusters: [],
  decisions: new Map(),
  activeFilter: "all",
  query: "",
  visible: [],
  activeClusterId: null,
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
  viewer: {zoom: 1, x: 0, y: 0, crop: "fit"},
  viewDensity: "comfortable",
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
  $("#preview-activity").textContent = active
    ? `${active} active` : progress.failed ? `${progress.failed} failed` : "Idle";
  $("#preview-activity").className =
    progress.failed ? "attention" : active ? "active" : "";
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
    state.previewPoll[size] = setTimeout(
      () => focusPreviews(visible, prefetch, size), 1000);
  }
}

async function retryPreview(name, size) {
  try {
    const result = await operationalPost("/api/previews/retry", {name, size});
    applyPreviewStates({previews: {[name]: result}});
    pollPreviews([name], size, state.previewGeneration[size]);
  } catch (error) {
    $("#preview-status").textContent = error.message;
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
  renderQueueOverview(state.jobs.jobs);
  const active = state.jobs.jobs.find((job) =>
    ["running", "stopping", "detached"].includes(job.status));
  const waiting = state.jobs.jobs.filter((job) => job.status === "queued").length;
  $("#queue-status").textContent = active
    ? `One supervised process is active${waiting ? ` · ${waiting} waiting` : ""}`
    : waiting ? `${waiting} waiting · worker will start automatically`
      : "Worker idle · ready for another folder";
  state.jobs.jobs.forEach((job, jobIndex) => {
    const presentation = jobStatusPresentation[job.status] || {
      label: job.status, tone: "neutral", stage: job.message,
    };
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
    title.textContent = job.photos.split("/").filter(Boolean).pop() || job.photos;
    const path = document.createElement("span");
    path.className = "muted";
    path.textContent = job.photos;
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
    const progressValue = Math.max(0, Math.min(1, Number(job.progress.fraction) || 0));
    percent.textContent = job.status === "completed"
      ? "100%" : `${Math.round(progressValue * 100)}%`;
    progressHeading.append(stage, percent);
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = job.status === "completed" ? 1 : progressValue;
    progress.setAttribute("aria-label", `${presentation.label} progress`);
    const progressText = document.createElement("p");
    progressText.className = "muted";
    progressText.textContent = job.progress.total_clusters
      ? `${job.progress.completed_clusters} of ${job.progress.total_clusters} clusters checkpointed`
      : "Waiting for the first validated checkpoint";
    progressBlock.append(progressHeading, progress, progressText);
    const detail = document.createElement("p");
    detail.className = "job-message";
    detail.textContent = job.message;

    const facts = document.createElement("div");
    facts.className = "job-facts";
    for (const [label, value] of [
      ["Profile", job.profile],
      ["Keep", `At most ${job.keep_per_group}`],
      ["Provider", job.provider_profile_name || "Legacy agents.kim"],
      ["Added", jobTime(job.created_at)],
    ]) {
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
      ["Recursive", job.recursive ? "yes" : "no"],
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
    const waitingBefore = state.jobs.jobs.slice(0, jobIndex).filter(
      (candidate) => candidate.status === "queued").length;
    queuePosition.textContent = job.status === "queued"
      ? `Queue position ${waitingBefore + (active ? 2 : 1)}`
      : `Job ${job.id}`;
    footer.append(queuePosition, actions);
    article.append(
      header, progressBlock, detail, facts, details, logs, footer);
    target.append(article);
  });
  if (!state.jobs.jobs.length) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    const mark = document.createElement("span");
    mark.textContent = "◎";
    mark.setAttribute("aria-hidden", "true");
    const heading = document.createElement("strong");
    heading.textContent = "The queue is ready";
    const message = document.createElement("p");
    message.textContent =
      "Choose a photo folder above. OpenCull will validate it before starting.";
    empty.append(mark, heading, message);
    target.append(empty);
  }
}

function setJobFormStatus(message, kind = "") {
  const target = $("#job-form-status");
  target.textContent = message;
  target.className = `job-form-status ${kind}`.trim();
}

function updateJobReadiness() {
  const photos = $("#job-photos").value.trim();
  const keepers = Number($("#job-keepers").value);
  const ready = Boolean(photos) && Number.isInteger(keepers) &&
    keepers >= 1 && keepers <= 20;
  $("#add-job").disabled = !ready;
  if (!photos) {
    setJobFormStatus("Choose a photo folder to begin.");
  } else if (!Number.isInteger(keepers) || keepers < 1 || keepers > 20) {
    setJobFormStatus("Maximum per group must be between 1 and 20.", "error");
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
    renderJobs();
  } catch (error) {
    setJobFormStatus(error.message, "error");
  }
  clearTimeout(state.jobPoll);
  if (schedule && $("#jobs-dialog").open) {
    state.jobPoll = setTimeout(() => refreshJobs(true), 1000);
  }
}

async function addJob() {
  setJobFormStatus("Validating folder, output, provider, and Kimiya program…", "working");
  $("#add-job").disabled = true;
  try {
    state.jobs = await operationalPost("/api/jobs/add", {
      photos: $("#job-photos").value.trim(),
      output: $("#job-output").value.trim(),
      keep_per_group: Number($("#job-keepers").value),
      recursive: $("#job-recursive").checked,
      profile: $("#job-profile").value,
      provider_profile_id: $("#job-provider").value,
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
      A: "google/gemini-2.5-flash",
      B: "openai/gpt-4.1-mini",
      C: "mistralai/mistral-small-3.2-24b-instruct",
      D: "qwen/qwen3-vl-30b-a3b-instruct",
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
      "Observed previews leave this Mac. OpenCull requires OpenRouter Zero Data Retention routing for every configured agent.",
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
  const legacy = document.createElement("option");
  legacy.value = "";
  legacy.textContent = "Legacy agents.kim";
  select.append(legacy);
  for (const profile of state.providers?.profiles || []) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent =
      `${profile.name} · ${profile.privacy} · credential ${profile.credential}`;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === selected)) {
    select.value = selected;
  }
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
    ["Review events", reviewStatus.history_events],
    ["Est. remaining", formatDuration(reviewStatus.estimated_remaining_seconds)],
  ];
  const target = $("#summary");
  $("#undo-review").disabled =
    !state.review.status.compatible || !state.review.history.length;
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
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cluster-item";
    button.dataset.id = cluster.cluster_id;
    button.setAttribute(
      "aria-label",
      `Cluster ${state.clusterWindowStart + windowIndex + 1} of ${state.visible.length}, ` +
      `${cluster.photos.length} photographs`);
    if (cluster.cluster_id === state.activeClusterId) button.classList.add("active");

    const title = document.createElement("strong");
    title.textContent = cluster.cluster_id;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${cluster.photos.length} photos · ${decision.photos.length} kept`;
    const markers = document.createElement("span");
    markers.className = "markers";
    for (const key of [
      "warning", "fallback", "empty", "oversized", "reviewed", "modified",
      "maybe", "rejected", "lowconfidence",
    ]) {
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
  const missing = cluster.photos.filter((name) => state.payload.missing_photos.includes(name));
  if (missing.length) {
    target.append(alert(`Missing source files: ${missing.join(", ")}`, "missing"));
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
  if (draft.reviewed && humanSelected) card.classList.add("human-selected");
  if (draft.reviewed && aiSelected && !humanSelected) card.classList.add("human-rejected");
  if (draft.reviewed && !aiSelected && humanSelected) card.classList.add("human-promoted");
  card.dataset.photoName = name;
  card.setAttribute(
    "aria-label",
    `Photo ${position + 1} of ${cluster.photos.length}: ${name}`,
  );
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
  card.querySelector(".photo-number").textContent = String(position + 1);
  card.querySelector(".keeper-badge").textContent =
    aiSelected ? "AI keeper" : "AI did not select";
  const humanBadge = card.querySelector(".human-badge");
  humanBadge.textContent = draft.reviewed
    ? (humanSelected ? "Kept by human" : "Not kept by human")
    : "Awaiting human review";
  const imageButton = card.querySelector(".image-button");
  imageButton.addEventListener("click", (event) => {
    if (!event.target.closest(".image-error")) openViewer([name]);
  });
  imageButton.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openViewer([name]);
    }
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
  toggle.setAttribute("aria-pressed", String(humanSelected));
  toggle.setAttribute(
    "aria-label",
    `${humanSelected ? "Remove" : "Keep"} ${name} (${position + 1})`,
  );
  if (position < 9) toggle.setAttribute("aria-keyshortcuts", String(position + 1));
  toggle.addEventListener("click", () => toggleHumanKeeper(cluster, name));
  const compare = card.querySelector(".compare-button");
  const comparisonSelected = state.compare.includes(name);
  if (comparisonSelected) compare.classList.add("active");
  compare.textContent = comparisonSelected ? "Comparing" : "Compare";
  compare.setAttribute("aria-pressed", String(comparisonSelected));
  compare.setAttribute("aria-label", `${comparisonSelected ? "Remove" : "Add"} ${name} ${
    comparisonSelected ? "from" : "to"} comparison`);
  compare.addEventListener("click", () => toggleCompare(name));
  const flag = card.querySelector(".photo-flag");
  const rating = card.querySelector(".photo-rating");
  const label = card.querySelector(".photo-label");
  flag.value = annotation.flag;
  rating.value = String(annotation.rating);
  label.value = annotation.label;
  const saveJudgment = () => {
    const current = draftFor(cluster);
    current.photo_annotations[name] = {
      flag: flag.value,
      rating: Number(rating.value),
      label: label.value,
    };
    if (flag.value === "keep" && !current.keepers.includes(name)) {
      current.keepers.push(name);
    } else if (flag.value === "reject") {
      current.keepers = current.keepers.filter((item) => item !== name);
    }
    current.reviewed = true;
    saveCluster(cluster, current, `${name} judgment saved`, "photo-judgment");
    showCluster(cluster.cluster_id);
  };
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

function showCluster(clusterId) {
  const cluster = state.clusters.find((item) => item.cluster_id === clusterId);
  if (!cluster) return;
  const decision = decisionFor(cluster);
  if (state.activeClusterId !== clusterId) state.compare = [];
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

async function acceptAIAndNext() {
  const clusterId = state.activeClusterId;
  const saved = await acceptAIRecommendation("AI recommendation accepted");
  if (saved && state.activeClusterId === clusterId) navigate(1);
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
  const move = action === "move";
  const trash = action === "trash";
  const badge = $("#operation-risk-badge");
  badge.textContent = trash
    ? "Unselected files go to Trash"
    : move ? "Source files will move" : "Verified copy";
  badge.className = `operation-risk-badge ${move || trash ? "move" : "copy"}`;
  $("#operation-risk-message").textContent = trash
    ? "Only unselected photographs are moved to the macOS Trash after verification. OpenCull retains a journal so the operation can be resumed or rolled back."
    : move
      ? "Move removes each source only after its destination copy passes SHA-256 verification. A rollback journal is retained."
      : "Copy creates SHA-256 verified duplicates and leaves every source photograph in place.";
  const scope = $("#operation-scope");
  const destination = $("#operation-destination");
  const picker = $("#pick-operation-destination");
  if (trash) scope.value = "unselected";
  scope.disabled = trash;
  destination.disabled = trash;
  picker.disabled = trash;
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
  $("#plan-summary").replaceChildren(
    planStat("Files", plan.summary.files),
    planStat("Selected", plan.summary.selected_files),
    planStat("Unselected", plan.summary.unselected_files),
    planStat("Companions", plan.summary.companion_files),
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
    if (action !== "trash" && !destination) {
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
  }
  if (operation.error) {
    $("#operation-progress-text").textContent += ` · ${operation.error}`;
  }
  const running = ["planned", "running", "paused"].includes(operation.status);
  $("#cancel-operation").hidden = !running;
  $("#rollback-operation").hidden =
    !["move", "trash"].includes(operation.action) ||
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
    thumb.src = imageUrl(candidate, "thumb");
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
    image.dataset.previewName = name;
    image.dataset.previewSize = "detail";
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
  $("#recovery-banner-title").textContent = title;
  $("#recovery-banner-message").textContent = message;
  banner.className = `recovery-banner ${severity}`;
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
    attention ? "OpenCull needs your attention" : "Review is healthy";
  $("#recovery-health-message").textContent = attention
    ? "Use the checks below to recover without losing review work."
    : "The report, source volume, and human review binding are available.";
}

function openRecoveryCenter(loadError = "") {
  renderRecoveryCenter(loadError);
  const dialog = $("#recovery-dialog");
  if (!dialog.open) dialog.showModal();
}

function switchWorkspace(mode) {
  state.workspaceMode = mode === "professional" ? "professional" : "review";
  const professional = state.workspaceMode === "professional";
  $("#review-workspace").hidden = professional;
  $("#professional-workspace").hidden = !professional;
  $("#summary").hidden = professional;
  $("#review-nav-button").classList.toggle("active", !professional);
  $("#review-nav-button").toggleAttribute("aria-current", !professional);
  $("#shortlist-nav-button").classList.toggle("active", professional);
  $("#shortlist-nav-button").toggleAttribute("aria-current", professional);
  if (professional) {
    renderShortlistWorkspace();
    const entry = activeShortlistEntry();
    if (entry) focusPreviews([entry.photo], [], "detail");
  }
}

function shortlistHumanEntry(photo) {
  return state.shortlistReview?.entries?.[photo] || null;
}

function effectiveShortlistEntry(entry) {
  const human = shortlistHumanEntry(entry.photo);
  const reviewed = Boolean(human?.reviewed);
  return {
    ...entry,
    effectiveTier: reviewed ? human.tier : entry.tier,
    editRaw: reviewed
      ? human.edit_raw : ["exceptional", "strong"].includes(entry.tier),
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

function renderShortlistList() {
  state.shortlistVisible = filteredShortlistEntries();
  const target = $("#shortlist-list");
  target.replaceChildren();
  $("#shortlist-visible-count").textContent =
    `${state.shortlistVisible.length} of ${state.shortlist?.entries.length || 0} photographs`;
  for (const entry of state.shortlistVisible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "shortlist-item";
    button.dataset.photo = entry.photo;
    if (entry.photo === state.activeShortlistPhoto) button.classList.add("active");
    const image = document.createElement("img");
    image.className = "shortlist-thumb";
    image.alt = "";
    image.loading = "lazy";
    image.src = imageUrl(entry.photo, "thumb");
    const copy = document.createElement("span");
    copy.className = "shortlist-item-copy";
    const name = document.createElement("strong");
    name.textContent = entry.photo.split("/").at(-1);
    const detail = document.createElement("span");
    detail.textContent = `${entry.effectiveTier} · ${Math.round(entry.score)} · ${
      entry.raw_files.length ? "RAW" : "JPEG only"}${
      entry.humanReviewed ? " · reviewed" : ""}`;
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
    target.append(button);
  }
}

function activeShortlistEntry() {
  return state.shortlist?.entries.find(
    (entry) => entry.photo === state.activeShortlistPhoto) || null;
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
  image.alt = entry.photo;
  image.dataset.previewName = entry.photo;
  image.dataset.previewSize = "detail";
  image.removeAttribute("src");
  $("#shortlist-open-viewer .image-loading").hidden = false;
  $("#shortlist-open-viewer .image-error").hidden = true;
  $("#shortlist-rationale").textContent = entry.rationale;
  $("#shortlist-cluster").textContent = entry.cluster_id;
  $("#shortlist-raw-files").textContent =
    entry.raw_files.join(", ") || "No associated RAW file";
  $("#shortlist-confidence").textContent =
    `${Math.round(Number(entry.confidence || 0) * 100)}%`;
  $("#shortlist-human-tier").value = entry.effectiveTier;
  $("#shortlist-edit-raw").checked = entry.editRaw;
  $("#shortlist-reviewed").checked = entry.humanReviewed;
  $("#shortlist-note").value = entry.humanNote;
  const compatible = !state.shortlistReview?.stale;
  for (const control of [
    $("#shortlist-human-tier"), $("#shortlist-edit-raw"),
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
  focusPreviews([entry.photo], [], "detail");
}

function showShortlistEntry(photo) {
  if (!state.shortlistVisible.some((entry) => entry.photo === photo)) return;
  state.activeShortlistPhoto = photo;
  renderShortlistList();
  renderShortlistDetail();
  requestAnimationFrame(() => {
    document.querySelector(
      `.shortlist-item[data-photo="${CSS.escape(photo)}"]`)
      ?.scrollIntoView({block: "nearest"});
  });
}

function renderShortlistWorkspace() {
  if (!state.shortlist) {
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
  try {
    const payload = await operationalPost("/api/shortlist/review", {
      photo: entry.photo,
      tier: $("#shortlist-human-tier").value,
      edit_raw: $("#shortlist-edit-raw").checked,
      reviewed: $("#shortlist-reviewed").checked,
      note: $("#shortlist-note").value,
      revision: state.shortlistReview.revision,
    });
    state.shortlistReview = payload;
    setSaveStatus("Professional shortlist decision saved");
    renderShortlistWorkspace();
    if (advance) navigateShortlist(1);
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
  if (!job) {
    panel.hidden = true;
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
  $("#review-nav-button").addEventListener(
    "click", () => switchWorkspace("review"));
  $("#shortlist-nav-button").addEventListener(
    "click", () => switchWorkspace("professional"));
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
  $("#open-shortlist-queue").addEventListener("click", () => {
    $("#jobs-dialog").showModal();
    refreshJobs();
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
    renderClusterList();
    updateNavigation();
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
  $("#accept-ai-next").addEventListener("click", acceptAIAndNext);
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
        image.removeAttribute("src");
      });
      const cluster = state.clusters.find(
        (item) => item.cluster_id === state.activeClusterId);
      if (cluster) focusPreviews(cluster.photos, [], "thumb");
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
      void acceptAIAndNext();
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
    $("#report-status").textContent =
      `${state.payload.summary.photos} photos · ${state.payload.summary.clusters} clusters`;
    if (!state.review.status.compatible) {
      setSaveStatus(state.review.status.stale_reason, "error");
      setRecoveryBanner(
        "Human review does not match this report",
        "OpenCull has kept the sidecar read-only. Review recovery options before continuing.",
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
