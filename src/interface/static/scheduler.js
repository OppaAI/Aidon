"use strict";

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

async function loadScheduledTasks() {
  const count = document.getElementById("scheduler-count");
  const list = document.getElementById("scheduler-list");
  const refresh = document.getElementById("scheduler-refresh");
  count.textContent = "Loading scheduled tasks…";
  refresh.disabled = true;
  try {
    const response = await fetch("/api/scheduled-tasks");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load scheduled tasks");
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    count.textContent = `${jobs.length} ${jobs.length === 1 ? "task" : "tasks"}`;
    if (!jobs.length) {
      list.innerHTML = '<div class="scheduler-empty">No scheduled tasks yet.</div>';
      return;
    }
    list.innerHTML = jobs.map(job => {
      const state = job.pending ? "Run pending" : (job.next_run ? "Scheduled" : "No upcoming run");
      const details = [
        ["Type", job.kind], ["Schedule", job.schedule], ["Timezone", job.timezone],
        ["Next run", job.pending ? "Pending dispatch" : (job.next_run || "None")],
        ["Last run", job.last_run || "Never"], ...(job.until ? [["Until", job.until]] : [])
      ];
      return '<article class="scheduler-card"><div class="scheduler-card-head"><h2>'
        + escapeHtml(job.name) + '</h2><span class="scheduler-badge">' + escapeHtml(state)
        + '</span></div><p class="scheduler-goal">' + escapeHtml(job.goal)
        + '</p><div class="scheduler-meta">' + details.map(([label, value]) =>
          '<span><strong>' + escapeHtml(label) + ':</strong> ' + escapeHtml(value) + '</span>').join("")
        + '</div></article>';
    }).join("");
  } catch (error) {
    count.textContent = "Could not load tasks";
    list.innerHTML = '<div class="scheduler-empty">' + escapeHtml(error.message) + '</div>';
  } finally {
    refresh.disabled = false;
  }
}

document.getElementById("scheduler-refresh").addEventListener("click", loadScheduledTasks);
loadScheduledTasks();
