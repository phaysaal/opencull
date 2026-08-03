/* Darkimiya launcher.
 *
 * Talks to the local desktop bridge. The per-launch token is written into the
 * page by the server rather than passed in the URL, where it would be logged
 * and kept in history.
 */

const TOKEN = window.__DARKIMIYA_TOKEN__ || "";
const $ = (id) => document.getElementById(id);

const state = { projects: [], jobs: [], providers: null, busy: false, home: "" };

async function api(path, body) {
  const options = {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${TOKEN}` },
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let value = {};
  try {
    value = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`The backend returned something unreadable (${response.status}).`);
  }
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status}).`);
  return value;
}

function notice(message, tone) {
  const element = $("notice");
  if (!message) {
    element.hidden = true;
    element.textContent = "";
    return;
  }
  element.hidden = false;
  element.textContent = message;
  if (tone) element.dataset.tone = tone; else delete element.dataset.tone;
}

function number(value) {
  return Number(value || 0).toLocaleString();
}

// A path is orientation, not evidence: show it the way the person writes it.
function shortPath(value) {
  const home = state.home;
  if (home && value.startsWith(home)) return `~${value.slice(home.length)}`;
  return value;
}

/* --- rendering ------------------------------------------------------- */

function jobState(job) {
  if (!job) return null;
  const status = String(job.status || "");
  if (status === "running") return { label: "Culling", tone: "running", running: true };
  if (status === "queued") return { label: "Queued", tone: "running", running: true };
  if (status === "failed") return { label: "Failed", tone: "failed" };
  if (status === "cancelled") return { label: "Cancelled" };
  return null;
}

function projectState(project) {
  const running = jobState(project.culling);
  if (running) return running;
  if (project.report_available) return { label: "Reviewed", tone: "ready" };
  if (!project.available) return { label: "Folder offline", tone: "failed" };
  return { label: "Not culled" };
}

function row({ name, path, count, state: rowState, actions, progress }) {
  const item = document.createElement("li");
  item.className = "row";
  if (rowState?.running) item.classList.add("is-running");

  const film = document.createElement("span");
  film.className = "row__film";
  film.setAttribute("aria-hidden", "true");
  item.append(film);

  const body = document.createElement("div");
  body.className = "row__body";

  const main = document.createElement("div");
  main.className = "row__main";
  const title = document.createElement("p");
  title.className = "row__name";
  title.textContent = name;
  main.append(title);
  if (path) {
    const location = document.createElement("p");
    location.className = "row__path";
    location.textContent = path;
    location.title = path;
    main.append(location);
  }
  if (typeof progress === "number") {
    const meter = document.createElement("div");
    meter.className = "meter";
    const fill = document.createElement("div");
    fill.className = "meter__fill";
    fill.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    meter.append(fill);
    main.append(meter);
  }
  body.append(main);

  if (count !== undefined && count !== null) {
    const frames = document.createElement("span");
    frames.className = "row__count";
    frames.textContent = count;
    body.append(frames);
  }

  if (rowState) {
    const badge = document.createElement("span");
    badge.className = "row__state";
    badge.textContent = rowState.label;
    if (rowState.tone) badge.dataset.tone = rowState.tone;
    body.append(badge);
  }

  if (actions?.length) {
    const group = document.createElement("div");
    group.className = "row__actions";
    for (const action of actions) {
      const button = document.createElement("button");
      button.className = "ghost";
      button.type = "button";
      button.textContent = action.label;
      button.disabled = Boolean(action.disabled);
      button.addEventListener("click", action.run);
      group.append(button);
    }
    body.append(group);
  }

  item.append(body);
  return item;
}

function renderLibrary() {
  const list = $("library");
  list.replaceChildren();
  const projects = state.projects;

  for (const project of projects) {
    const rowState = projectState(project);
    const actions = [];
    if (project.report_available) {
      actions.push({ label: "Open review", run: () => openReview(project) });
    }
    if (!rowState.running && project.available) {
      actions.push({
        label: project.report_available ? "Cull again" : "Cull",
        run: () => cullProject(project),
      });
    }
    list.append(row({
      name: project.name,
      path: shortPath(project.photos),
      state: rowState,
      actions,
      progress: rowState.running ? jobProgress(project.culling) : undefined,
    }));
  }
}

function jobProgress(job) {
  if (!job) return undefined;
  const match = String(job.log_tail || "").match(/(\d{1,3})\s*%/g);
  if (match?.length) return Number(match[match.length - 1].replace(/\D/g, ""));
  return job.status === "running" ? 0 : undefined;
}

function renderQueue() {
  const active = state.jobs.filter(
    (job) => job.status === "running" || job.status === "queued");
  const band = $("queue-band");
  band.hidden = active.length === 0;
  const list = $("queue");
  list.replaceChildren();
  for (const job of active) {
    const info = jobState(job);
    list.append(row({
      name: job.name || job.kind || "Job",
      path: shortPath(job.photos || ""),
      state: info,
      progress: jobProgress(job),
      actions: [{ label: "Cancel", run: () => jobAction(job, "cancel") }],
    }));
  }
}

function renderHero() {
  // The count is folders, which the catalogue actually knows. Frame totals
  // would mean walking every folder on each poll.
  const folders = state.projects.length;
  const reviewed = state.projects.filter((p) => p.report_available).length;
  const running = state.projects.filter((p) => projectState(p).running).length;
  const empty = folders === 0;

  // An empty library should invite the first folder, not announce a zero.
  document.body.classList.toggle("is-empty", empty);
  $("hero-count").hidden = empty;
  $("hero-lead").hidden = !empty;
  $("library-band").hidden = empty;

  if (empty) {
    $("hero-sub").textContent =
      "Point Darkimiya at a shoot. It groups the near-duplicate frames and "
      + "proposes keepers. Nothing is moved or deleted.";
    return;
  }
  $("frame-total").textContent = number(folders);
  const parts = [folders === 1 ? "folder" : "folders"];
  if (running) parts.push(`${number(running)} culling now`);
  else if (reviewed) parts.push(`${number(reviewed)} reviewed`);
  $("hero-sub").textContent = parts.join(" · ");
}

function renderProviders() {
  const providers = state.providers;
  const profile = providers?.profiles?.[0];
  const stored = profile?.credential === "stored";
  const input = $("provider-key");
  if (stored) {
    input.placeholder = "•".repeat(24);
    input.classList.add("has-stored");
  } else {
    input.placeholder = "Paste your provider API key";
    input.classList.remove("has-stored");
  }
  if (profile) $("provider-name").value = profile.name;
  const storage = providers?.credential_storage;
  $("provider-key-help").textContent = stored
    ? `A key is stored${storage ? ` in ${storage.label}` : ""}. `
      + "Leave this blank to keep it."
    : "The key is written to this machine's credential store, never to settings.";
}

function render() {
  renderHero();
  renderLibrary();
  renderQueue();
  renderProviders();
}

/* --- actions --------------------------------------------------------- */

async function guard(run, working) {
  if (state.busy) return;
  state.busy = true;
  notice(working || "");
  try {
    await run();
    notice("");
  } catch (error) {
    notice(error.message, "alarm");
  } finally {
    state.busy = false;
    await refresh();
  }
}

function cullFolder() {
  return guard(async () => {
    const chosen = await api("/choose-folder", {});
    if (!chosen.path) return;
    await api("/projects", { photos: chosen.path });
    const project = (await api("/state")).projects.projects.find(
      (item) => item.photos === chosen.path);
    if (project) await api("/projects/cull", { project_id: project.id });
  }, "Choosing a folder…");
}

function cullProject(project) {
  return guard(
    () => api("/projects/cull", { project_id: project.id }),
    `Starting a cull of ${project.name}…`);
}

function openReview(project) {
  return guard(
    () => api("/projects/open", { project_id: project.id }),
    `Opening ${project.name}…`);
}

function openExisting() {
  return guard(async () => {
    const folder = await api("/choose-folder", {});
    if (!folder.path) return;
    const report = await api("/choose-report", {});
    if (!report.path) return;
    await api("/projects/import-report", {
      photos: folder.path, report: report.path });
  }, "Choosing a folder and report…");
}

function jobAction(job, action) {
  return guard(() => api("/jobs/action", { job_id: job.id, action }));
}

async function saveProvider() {
  const status = $("provider-status");
  status.textContent = "Saving…";
  delete status.dataset.tone;
  try {
    const existing = state.providers?.profiles?.[0];
    const profile = {
      name: $("provider-name").value.trim() || "OpenRouter",
      kind: "openrouter",
      models: {
        A: "openai/gpt-5.6-luna-pro",
        B: "openai/gpt-5.6-luna-pro",
        C: "openai/gpt-4.1-mini",
        D: "openai/gpt-5.6-luna-pro",
      },
      credential_required: true,
      zdr: true,
      cost_note: "",
    };
    if (existing) profile.id = existing.id;
    await api("/providers/save", {
      profile,
      revision: state.providers.revision,
      secret: $("provider-key").value,
    });
    $("provider-key").value = "";
    status.textContent = "Saved.";
    status.dataset.tone = "ok";
    await refresh();
  } catch (error) {
    status.textContent = error.message;
    status.dataset.tone = "alarm";
  }
}

/* --- polling --------------------------------------------------------- */

async function refresh() {
  try {
    const payload = await api("/state");
    state.projects = payload.projects?.projects || [];
    state.jobs = payload.queue?.jobs || [];
    state.providers = payload.providers || null;
    state.home = payload.home || state.home;
    render();
  } catch (error) {
    notice(error.message, "alarm");
  }
}

function start() {
  $("cull-folder").addEventListener("click", cullFolder);
  $("open-existing").addEventListener("click", openExisting);
  $("open-providers").addEventListener("click", () => {
    renderProviders();
    $("providers-sheet").showModal();
  });
  $("save-provider").addEventListener("click", saveProvider);
  refresh();
  setInterval(refresh, 2000);
}

document.addEventListener("DOMContentLoaded", start);
