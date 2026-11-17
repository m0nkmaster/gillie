function asText(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function buildSkillLogs(tasks, activity, diagnostics = "", fetchError = "") {
  const skillRuns = (Array.isArray(tasks) ? tasks : [])
    .map((task) => ({
      id: String(task.id || ""),
      skill: String(task.skill || task.id || ""),
      status: String(task.status || "idle"),
      lastRun: String(task.last_run || ""),
      duration: Number(task.last_duration || 0),
      runCount: Number(task.run_count || 0),
      summary: asText(task.last_output?.summary),
      details: asText(task.last_output?.details),
      error: asText(task.last_error),
    }))
    .sort((a, b) => b.lastRun.localeCompare(a.lastRun));

  const recentRuns = (Array.isArray(activity) ? activity : [])
    .map((entry) => ({
      id: String(entry.id || ""),
      label: String(entry.label || ""),
      source: String(entry.source || ""),
      status: String(entry.status || ""),
      started: String(entry.started || ""),
      duration: Number(entry.duration || 0),
    }))
    .sort((a, b) => b.started.localeCompare(a.started));

  return {
    skillRuns,
    recentRuns,
    diagnostics: String(diagnostics || ""),
    fetchError: String(fetchError || ""),
  };
}

function formatSkillLogs(logs) {
  const lines = ["ASSISTANT SKILL LOGS", ""];
  if (logs.fetchError) lines.push(`Error: ${logs.fetchError}`, "");
  for (const run of logs.skillRuns) {
    lines.push(`${run.id} [${run.status}]`);
    lines.push(`Skill: ${run.skill}`);
    lines.push(`Last run: ${run.lastRun || "never"} | Duration: ${run.duration}s | Runs: ${run.runCount}`);
    if (run.error) lines.push(`Error: ${run.error}`);
    if (run.summary) lines.push(`Summary:\n${run.summary}`);
    if (run.details) lines.push(`Details:\n${run.details}`);
    lines.push("");
  }
  lines.push("RECENT RUNS", "");
  for (const run of logs.recentRuns) {
    lines.push(`${run.started} ${run.label} [${run.status}] ${run.duration}s (${run.source})`);
  }
  return lines.join("\n");
}

module.exports = { buildSkillLogs, formatSkillLogs };
