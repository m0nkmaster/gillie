const assert = require("node:assert/strict");
const test = require("node:test");
const { NotificationState } = require("../src/notification-state");

test("new board items notify once across saved state", () => {
  const state = new NotificationState();
  const item = { id: "item-1", new: true, title: "Review needed", why: "A PR is waiting" };

  const [notice] = state.observeBoard([item]);
  assert.deepEqual(notice, {
    kind: "item",
    id: "item-1",
    title: "Review needed",
    body: "A PR is waiting",
    route: "/assistant?item=item-1",
  });
  assert.deepEqual(state.observeBoard([item]), []);
  state.confirm(notice);

  const restored = new NotificationState(state.toJSON());
  assert.deepEqual(restored.observeBoard([item]), []);
});

test("new board items notify even while the window is focused", () => {
  const state = new NotificationState();
  const item = { id: "item-1", new: true, title: "Visible now" };

  assert.equal(state.observeBoard([item], true).length, 1);
});

test("failed notification delivery is released for retry", () => {
  const state = new NotificationState();
  const item = { id: "item-1", new: true, title: "Try again" };

  const [notice] = state.observeBoard([item]);
  state.release(notice);

  assert.equal(state.observeBoard([item]).length, 1);
});

test("legacy seen state is retried because delivery was never confirmed", () => {
  const state = new NotificationState({ seenItems: ["item-1"] });
  const item = { id: "item-1", new: true, title: "Retry after upgrade" };

  assert.equal(state.observeBoard([item]).length, 1);
});

test("a sign-in wait notifies once per reason until it clears", () => {
  const state = new NotificationState();
  const waiting = [
    { id: "watch-a", status: "blocked", last_error: "Waiting for sign-in: Teams" },
    { id: "watch-b", status: "blocked", last_error: "Waiting for sign-in: Teams" },
  ];

  assert.deepEqual(state.observeTasks([{ id: "watch-a", status: "error" }]), []);
  const notices = state.observeTasks(waiting);
  assert.equal(notices.length, 1);
  assert.equal(notices[0].title, "Sign-in needed");
  assert.deepEqual(state.observeTasks(waiting), []);
  state.confirm(notices[0]);
  assert.deepEqual(state.observeTasks(waiting), []);
  state.observeTasks([{ id: "watch-a", status: "success" }]);
  assert.equal(state.observeTasks(waiting).length, 1);
});

test("an undelivered sign-in notice is retried", () => {
  const state = new NotificationState();
  const waiting = [{ id: "watch-a", status: "blocked", last_error: "Waiting for sign-in: Teams" }];
  const [notice] = state.observeTasks(waiting);
  state.release(notice);
  assert.equal(state.observeTasks(waiting).length, 1);
});

test("runner failures notify once until recovery", () => {
  const state = new NotificationState();

  const [notice] = state.beginRunnerFailure("stopped");
  assert.ok(notice);
  assert.deepEqual(state.beginRunnerFailure("still stopped"), []);
  state.confirm(notice);
  state.clearRunnerFailure();
  assert.equal(state.beginRunnerFailure("stopped again").length, 1);
});

test("notices carry deep-link routes for desktop notification clicks", () => {
  const { routeForNotice } = require("../src/notification-state");
  const state = new NotificationState();
  const [item] = state.observeBoard([
    { id: "ticket-123", new: true, title: "Ticket", why: "Blocked" },
  ]);
  assert.equal(item.route, "/assistant?item=ticket-123");
  assert.equal(
    routeForNotice(item),
    "/assistant?item=ticket-123",
  );
  const [auth] = state.observeTasks([
    { id: "watch-a", status: "blocked", last_error: "Waiting for sign-in: Teams" },
  ]);
  assert.equal(auth.route, "/runner?task=watch-a");
  const [runner] = state.beginRunnerFailure("stopped");
  assert.equal(runner.route, "/runner");
  assert.equal(routeForNotice(runner), "/runner");
});

test("notice keys round-trip so a later process can route a click", () => {
  const { noticeKey, noticeFromKey, routeForNotice } = require("../src/notification-state");
  const notice = { kind: "item", id: "teams:thread:42", title: "x" };
  assert.deepEqual(noticeFromKey(noticeKey(notice)), { kind: "item", id: "teams:thread:42" });
  assert.equal(routeForNotice(noticeFromKey("item:abc")), "/assistant?item=abc");
  assert.equal(noticeFromKey("bogus"), null);
  assert.equal(noticeFromKey("other:abc"), null);
  assert.equal(noticeFromKey(undefined), null);
});
