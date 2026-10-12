"use strict";

// Scripted provider responses only. This module never opens a database or starts
// an agent. capture.js serves these fixtures to the unmodified product frontend.
const cwd = "/demo/playground";
const epoch = 1791583200;
const ids = {dispatcher: "demo-dispatcher", chat: "demo-chat", snake: "demo-snake", pong: "demo-pong"};
const prompts = [
  {target: "chat", text: "Build a minimalist chat app.", title: "Chat app", summary: "Building the conversation view and message composer.", response: "I’m building a clean conversation view with a message composer."},
  {target: "snake", text: "Build a Snake game.", title: "Snake", summary: "Building the game loop, keyboard controls, and score.", response: "I’m setting up the game loop, controls, and score."},
  {target: "pong", text: "Build a two-player Pong game.", title: "Pong", summary: "Building the paddles, ball physics, and scoring.", response: "I’m building the two-player controls and ball physics."},
  {target: "snake", text: "Make Snake work with swipe controls on my phone.", summary: "Adding swipe controls and a responsive game board.", response: "I’ll add swipe controls to the game I’m already building, and size the board for phones."},
  {target: "chat", text: "Give the chat app a dark theme.", summary: "Adding the dark theme to the chat interface.", response: "I’ll use a dark palette for the chat interface and keep the contrast readable."},
  {target: "pong", text: "Add a rematch button to Pong.", summary: "Adding rematches while keeping the player scores visible.", response: "I’ll add a rematch button to the end-of-game screen."},
];
const clone = value => JSON.parse(JSON.stringify(value));
function thread(key, summary, index = 0) {
  const dispatcher = key === "dispatcher", stamp = epoch + index;
  const title = dispatcher ? "Playground dispatcher" : prompts.find(p => p.target === key).title;
  const phase = dispatcher ? "orchestrating" : "implementation";
  return {thread_id: ids[key], session_id: ids[key], title, title_source: "agent", cwd, project_cwd: cwd,
    client: "codex", browser_session: true, dispatcher, attention: "now", work_phase: phase,
    status: dispatcher ? "idle" : "running", response_state: dispatcher ? "idle" : "working",
    created_at: epoch, updated_at: stamp, last_work_at: stamp, turn_active: !dispatcher,
    needs_attention: false, unread: false, unread_result: false, can_handle_response: false,
    original_request: dispatcher ? "Route requests to the right app agent." : prompts.find(p => p.target === key).text,
    checkpoint: {id: index + 1, phase, summary, next_action: "", next_actor: dispatcher ? "nobody" : "agent", author: "agent", created_at: stamp, refreshed_at: stamp},
    checkpoints: [], links: [], children: [], can_resume: false, can_fork: false};
}
function snapshot(count = 0, routing = false) {
  const requests = prompts.slice(0, count), keys = [...new Set(requests.map(p => p.target))];
  const threads = [thread("dispatcher", "Routes new ideas and follow-ups to the right conversation.")];
  for (const key of keys) {
    const index = requests.findLastIndex(p => p.target === key);
    threads.push(thread(key, requests[index].summary, index + 1));
  }
  const receipts = requests.map((p, i) => ({id: "receipt-" + i, dispatcher_id: ids.dispatcher, target_id: ids[p.target],
    target_title: threads.find(t => t.thread_id === ids[p.target]).title, state: "accepted", created_at: epoch + i * 10,
    body: {message: p.text}, turn_id: "turn-" + i}));
  if (routing) receipts.push({id: "routing-" + count, dispatcher_id: ids.dispatcher, state: "routing", created_at: epoch + count * 10,
    body: {message: prompts[count].text}});
  const details = {};
  for (const work of threads) {
    const key = Object.keys(ids).find(k => ids[k] === work.thread_id);
    const turns = [], messages = [];
    if (key !== "dispatcher") requests.forEach((p, i) => {
      if (p.target !== key) return;
      const first = i < 3;
      messages.push({id: i + 1, direction: "received", classification: "action_required", reply_required: false,
        created_at: epoch + i * 10, delivered_at: epoch + i * 10 + 1, body: p.text, status: "Delivered to agent",
        turn_id: "turn-" + i, counterpart: {title: "Playground dispatcher", session_id: ids.dispatcher, available: true}});
      turns.push({id: "turn-" + i, status: "inProgress", startedAt: epoch + i * 10 + 1,
        items: [{id: "reply-" + i, type: "agentMessage", text: p.response},
          {id: "thinking-" + i, type: "reasoning", summary: [first ? "Laying out the first version." : "Updating the existing implementation."], status: "completed"},
          {id: "work-" + i, type: "commandExecution", status: "inProgress", command: "npm run dev", aggregatedOutput: "Local preview ready."}]});
    });
    for (const previous of turns.slice(0, -1)) {
      previous.status = "completed";
      previous.completedAt = previous.startedAt + 8;
      previous.items.filter(item => item.type === "commandExecution").forEach(item => {item.status = "completed"; item.exitCode = 0;});
    }
    details[work.thread_id] = {session: {thread_id: work.thread_id, name: work.title, cwd, client: "codex", model: "gpt-6.1-sol", effort: "medium", yolo: true},
      work_thread: work, thread: {id: work.thread_id, turns}, requests: [], queuedMessages: [],
      running: key !== "dispatcher", activeTurn: key !== "dispatcher" ? turns.at(-1)?.id : null,
      coordinationMessages: messages, dispatchRequests: key === "dispatcher" ? receipts : []};
  }
  return clone({threads, details});
}
module.exports = {cwd, epoch, ids, prompts, snapshot};
