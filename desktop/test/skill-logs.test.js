const assert = require("node:assert/strict");
const test = require("node:test");
const { buildSkillLogs, formatSkillLogs } = require("../src/skill-logs");

test("skill logs prioritise task output and keep runner diagnostics secondary", () => {
  const logs = buildSkillLogs([
    {
      id: "assistant",
      skill: "assistant",
      status: "success",
      last_run: "2026-07-27T15:00:00Z",
      last_duration: 12.5,
      run_count: 4,
      last_output: { summary: "Found two items", details: { items: 2 } },
      last_error: "",
    },
  ], [
    {
      id: "run-1",
      label: "assistant",
      source: "scheduled",
      status: "done",
      started: "2026-07-27T15:00:00Z",
      duration: 12.5,
    },
  ], "uvicorn line");

  assert.equal(logs.skillRuns[0].summary, "Found two items");
  assert.equal(logs.skillRuns[0].details, '{\n  "items": 2\n}');
  assert.equal(logs.recentRuns[0].label, "assistant");
  assert.equal(logs.diagnostics, "uvicorn line");
  const copied = formatSkillLogs(logs);
  assert.ok(copied.includes("Found two items"));
  assert.ok(copied.includes("RECENT RUNS"));
  assert.ok(!copied.includes("uvicorn line"));
});
