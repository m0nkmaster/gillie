const MAX_SEEN_ITEMS = 5000;

function routeForNotice(notice) {
  if (notice.kind === "item") {
    return `/assistant?item=${encodeURIComponent(notice.id)}`;
  }
  if (notice.kind === "failure" && notice.id === "runner") {
    return "/runner";
  }
  if (notice.kind === "failure") {
    return `/runner?task=${encodeURIComponent(notice.id)}`;
  }
  return null;
}

// A delivered notification's macOS identifier. It must carry enough to rebuild the
// route, because a click can arrive in a later process that never saw the notice.
function noticeKey(notice) {
  return `${notice.kind}:${notice.id}`;
}

function noticeFromKey(key) {
  const text = typeof key === "string" ? key : "";
  const split = text.indexOf(":");
  if (split <= 0) return null;
  const kind = text.slice(0, split);
  const id = text.slice(split + 1);
  if (!id || (kind !== "item" && kind !== "failure")) return null;
  return { kind, id };
}

class NotificationState {
  constructor(saved = {}) {
    const confirmedState = saved.version === 2 ? saved : {};
    this.seenItems = new Set(
      Array.isArray(confirmedState.seenItems) ? confirmedState.seenItems : [],
    );
    this.activeFailures = new Set(
      Array.isArray(confirmedState.activeFailures) ? confirmedState.activeFailures : [],
    );
    this.pending = new Set();
  }

  _key(notice) {
    return noticeKey(notice);
  }

  _reserve(notice) {
    const key = this._key(notice);
    if (this.pending.has(key)) return false;
    this.pending.add(key);
    return true;
  }

  confirm(notice) {
    this.pending.delete(this._key(notice));
    if (notice.kind === "item") {
      this.seenItems.add(notice.id);
      while (this.seenItems.size > MAX_SEEN_ITEMS) {
        this.seenItems.delete(this.seenItems.values().next().value);
      }
    } else {
      this.activeFailures.add(notice.episode || notice.id);
    }
  }

  release(notice) {
    this.pending.delete(this._key(notice));
  }

  observeBoard(items) {
    const notices = [];
    for (const item of Array.isArray(items) ? items : []) {
      const id = typeof item.id === "string" ? item.id : "";
      if (!id || !item.new || this.seenItems.has(id)) continue;
      const notice = {
        kind: "item",
        id,
        title: String(item.title || "Roosta"),
        body: String(item.why || "Something needs your attention"),
        route: `/assistant?item=${encodeURIComponent(id)}`,
      };
      if (this._reserve(notice)) notices.push(notice);
    }
    return notices;
  }

  // A task held "blocked" is waiting for a sign-in. Notify once per reason (several tasks
  // usually wait on the same tool) and forget it when no task is waiting on it any more.
  observeTasks(tasks) {
    const notices = [];
    const waiting = new Map();
    for (const task of Array.isArray(tasks) ? tasks : []) {
      if (task.status !== "blocked") continue;
      const reason = String(task.last_error || "A sign-in is needed");
      if (!waiting.has(reason)) waiting.set(reason, task);
    }
    for (const [reason, task] of waiting) {
      const episode = `signin:${reason}`;
      if (this.activeFailures.has(episode)) continue;
      const notice = {
        kind: "failure",
        id: task.id,
        episode,
        title: "Sign-in needed",
        body: reason,
        route: `/runner?task=${encodeURIComponent(task.id)}`,
      };
      if (this._reserve(notice)) notices.push(notice);
    }
    for (const key of [...this.activeFailures]) {
      if (key.startsWith("signin:") && !waiting.has(key.slice(7))) this.activeFailures.delete(key);
    }
    return notices;
  }

  beginRunnerFailure(body) {
    if (this.activeFailures.has("runner")) return [];
    const notice = {
      kind: "failure",
      id: "runner",
      title: "Roosta runner stopped",
      body: String(body || "Open Roosta to restart it"),
      route: "/runner",
    };
    return this._reserve(notice) ? [notice] : [];
  }

  clearRunnerFailure() {
    this.activeFailures.delete("runner");
    this.pending.delete("failure:runner");
  }

  toJSON() {
    return {
      version: 2,
      seenItems: [...this.seenItems],
      activeFailures: [...this.activeFailures],
    };
  }
}

module.exports = { NotificationState, routeForNotice, noticeKey, noticeFromKey };
