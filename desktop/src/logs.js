const output = document.getElementById("output");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function relativeTime(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function render(data) {
  output.replaceChildren();
  if (data.fetchError) output.appendChild(element("div", "error", data.fetchError));

  output.appendChild(element("h2", "", "Latest skill output"));
  const skills = element("section", "skills");
  for (const run of data.skillRuns) {
    const card = element("article", "skill");
    const head = element("div", "skill-head");
    const identity = element("div");
    identity.append(
      element("div", "skill-name", run.id),
      element("div", "skill-sub", run.skill),
    );
    head.append(
      identity,
      element("span", `status status-${run.status}`, run.status),
    );
    card.append(
      head,
      element(
        "div",
        "meta",
        `Last run ${relativeTime(run.lastRun)} · ${run.duration || 0}s · ${run.runCount} total`,
      ),
    );
    if (run.error) card.appendChild(element("pre", "error", run.error));
    if (run.summary) card.appendChild(element("div", "output", run.summary));
    if (run.details) card.appendChild(element("pre", "", run.details));
    skills.appendChild(card);
  }
  if (!data.skillRuns.length) skills.appendChild(element("div", "meta", "No skill output available."));
  output.appendChild(skills);

  output.appendChild(element("h2", "", "Recent runs"));
  const recent = element("section");
  for (const run of data.recentRuns) {
    const row = element("div", "run");
    const copy = element("div");
    copy.append(
      element("div", "", run.label),
      element("div", "run-meta", `${relativeTime(run.started)} · ${run.source} · ${run.duration || 0}s`),
    );
    row.append(copy, element("span", `status status-${run.status}`, run.status));
    recent.appendChild(row);
  }
  if (!data.recentRuns.length) recent.appendChild(element("div", "meta", "No recent runs."));
  output.appendChild(recent);

  const diagnostics = element("details");
  diagnostics.append(
    element("summary", "", "Runner diagnostics"),
    element("pre", "", data.diagnostics || "No runner diagnostics."),
  );
  output.appendChild(diagnostics);
}

async function refresh() {
  output.textContent = "Loading...";
  render(await window.assistantDesktop.readLogs());
}

document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("copy").addEventListener("click", async (event) => {
  await window.assistantDesktop.copyLogs();
  event.target.textContent = "Copied";
  setTimeout(() => {
    event.target.textContent = "Copy";
  }, 1200);
});
document.getElementById("runner").addEventListener("click", () => window.assistantDesktop.openRunnerStatus());

refresh();
