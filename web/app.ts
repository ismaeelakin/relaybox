export {};
type State = "ready" | "leased" | "done" | "dead";
interface Job { id: string; state: State; attempts: number; max_attempts: number; payload: unknown; last_error: string | null; }
interface Snapshot { counts: Record<State, number>; jobs: Job[]; }

const element = <T extends HTMLElement>(id: string): T => {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing element: ${id}`);
  return found as T;
};
const queue = element<HTMLInputElement>("queue");
const status = element<HTMLParagraphElement>("status");
const tbody = element<HTMLTableSectionElement>("jobs");
let refreshVersion = 0;

async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? {cache: "no-store"} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  });
  const data = await response.json() as T & {error?: string};
  if (!response.ok) throw new Error(data.error ?? `HTTP ${response.status}`);
  return data;
}

function showError(error: unknown): void {
  status.textContent = error instanceof Error ? error.message : "Request failed";
  status.className = "error";
}

async function refresh(): Promise<void> {
  const version = ++refreshVersion;
  const name = queue.value;
  try {
    const snapshot = await api<Snapshot>(`/api/jobs?queue=${encodeURIComponent(name)}`);
    if (version !== refreshVersion) return;
    for (const state of ["ready", "leased", "done", "dead"] as const) {
      element(`count-${state}`).textContent = String(snapshot.counts[state]);
    }
    tbody.replaceChildren();
    for (const job of snapshot.jobs) {
      const row = document.createElement("tr");
      const id = document.createElement("td");
      id.textContent = job.id.slice(0, 8);
      id.title = job.id;
      const state = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `badge ${job.state}`;
      badge.textContent = job.state;
      state.append(badge);
      const attempt = document.createElement("td");
      attempt.textContent = `${job.attempts} / ${job.max_attempts}`;
      const payload = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = JSON.stringify(job.payload);
      payload.append(code);
      if (job.last_error) {
        const detail = document.createElement("small");
        detail.textContent = job.last_error;
        payload.append(detail);
      }
      const action = document.createElement("td");
      if (job.state === "dead") {
        const button = document.createElement("button");
        button.className = "subtle";
        button.textContent = "Replay";
        button.addEventListener("click", () => void run(button, async () => {
          await api(`/api/jobs/${job.id}/retry`, {});
        }));
        action.append(button);
      }
      row.append(id, state, attempt, payload, action);
      tbody.append(row);
    }
    element("empty").hidden = snapshot.jobs.length !== 0;
    status.className = "";
    status.textContent = `Queue ${name} · refreshed ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (version === refreshVersion) showError(error);
  }
}

async function run(button: HTMLButtonElement, action: () => Promise<void>): Promise<void> {
  button.disabled = true;
  try { await action(); await refresh(); } catch (error) { showError(error); }
  finally { button.disabled = false; }
}

element<HTMLFormElement>("enqueue").addEventListener("submit", event => {
  event.preventDefault();
  const button = element<HTMLButtonElement>("submit");
  void run(button, async () => {
    const payload: unknown = JSON.parse(element<HTMLTextAreaElement>("payload").value);
    const key = element<HTMLInputElement>("key").value.trim();
    await api("/api/jobs", {queue: queue.value, payload, ...(key ? {idempotency_key: key} : {})});
  });
});
element<HTMLButtonElement>("refresh").addEventListener("click", () => void refresh());
queue.addEventListener("change", () => void refresh());
element<HTMLButtonElement>("process").addEventListener("click", event => {
  void run(event.currentTarget as HTMLButtonElement, async () => {
    const {job} = await api<{job: (Job & {lease_token: string}) | null}>("/api/claim", {queue: queue.value});
    if (job) await api(`/api/jobs/${job.id}/ack`, {lease_token: job.lease_token});
  });
});
element<HTMLButtonElement>("fail").addEventListener("click", event => {
  void run(event.currentTarget as HTMLButtonElement, async () => {
    const {job} = await api<{job: (Job & {lease_token: string}) | null}>("/api/claim", {queue: queue.value});
    if (job) await api(`/api/jobs/${job.id}/fail`, {lease_token: job.lease_token, error: "Demo worker: simulated failure"});
  });
});
void refresh();
