/* shared.js — Reltio User Provisioning shared UI code
   Loaded by index.html, index_local.html, and demo_mock.html.

   Expects two globals to be defined BEFORE this script loads:
     CONFIG  — per-file configuration (shape varies by file)
     Backend — { mode, init, signIn, signOut, get, post, upload, openAttachment }

   Functions declared here use `function` declarations so per-file scripts
   loaded AFTER shared.js can override them (e.g. demo_mock.html redefines
   renderDerived, refreshReviewNow, etc.).
*/

// ---------------------------------------------------------------------------
// Theme — restore preference and wire toggle
// ---------------------------------------------------------------------------
(function () {
  var s = localStorage.getItem("theme");
  if (s === "dark" || s === "light") document.documentElement.setAttribute("data-theme", s);
})();
document.getElementById("themeToggle")?.addEventListener("click", function () {
  var isDark =
    document.documentElement.getAttribute("data-theme") === "dark" ||
    (!document.documentElement.getAttribute("data-theme") &&
      window.matchMedia("(prefers-color-scheme:dark)").matches);
  var next = isDark ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
});

// ---------------------------------------------------------------------------
// Core utilities
// ---------------------------------------------------------------------------
// eslint-disable-next-line no-unused-vars
var $ = function (id) { return document.getElementById(id); };
// eslint-disable-next-line no-unused-vars
var esc = function (s) {
  return String(s ?? "").replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
};

function pillIcon(s) {
  var i = {
    PENDING:
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    PROVISIONING:
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>',
    COMPLETED:
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>',
    REJECTED:
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    FAILED:
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>',
    CANCELLED:
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><path d="M8 12h8"/></svg>',
  };
  return i[s] || "";
}

var STATUS = {
  PENDING: "Waiting for approval",
  PROVISIONING: "Granting access",
  COMPLETED: "Access granted",
  REJECTED: "Rejected",
  FAILED: "Couldn't grant access",
  CANCELLED: "Cancelled",
};

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------
var state = {
  me: null, meta: null, resolved: null, file: null, uploadedKey: null,
  view: "new", returnView: "mine", wired: false,
};
var decisionDraft = null;
var approvalRefreshTimer = null;
var notifTimer = null;
var enterSeq = 0;

// ---------------------------------------------------------------------------
// Bug fix: clean up timers on page unload
// ---------------------------------------------------------------------------
window.addEventListener("beforeunload", function () {
  if (notifTimer) { clearInterval(notifTimer); notifTimer = null; }
  if (approvalRefreshTimer) { clearInterval(approvalRefreshTimer); approvalRefreshTimer = null; }
});

// ---------------------------------------------------------------------------
// Bug fix: AbortController for stale fetches
// ---------------------------------------------------------------------------
var _fetchSeq = {};
function _nextFetchSeq(key) { return (_fetchSeq[key] = (_fetchSeq[key] || 0) + 1); }
function _isStaleFetch(key, seq) { return _fetchSeq[key] !== seq; }

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function shimmerSkeleton(count) {
  var items = [];
  for (var i = 0; i < (count || 3); i++) {
    items.push('<div style="padding:1rem 1.15rem;border:1px solid var(--glass-border);border-radius:var(--radius);background:var(--glass);margin-bottom:.65rem;animation:fadeIn .3s both;animation-delay:' + (i * 0.08) + 's">' +
      '<div style="height:.9rem;width:' + (45 + (i % 3) * 15) + '%;background:linear-gradient(90deg,var(--surface) 25%,var(--surface-hover) 50%,var(--surface) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:4px;margin-bottom:.6rem"></div>' +
      '<div style="height:.7rem;width:' + (60 + (i % 2) * 20) + '%;background:linear-gradient(90deg,var(--surface) 25%,var(--surface-hover) 50%,var(--surface) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:4px"></div></div>');
  }
  return items.join("");
}

function listNames(names, conj) {
  if (conj === undefined) conj = "and";
  if (names.length <= 1) return names[0] || "";
  return names.slice(0, -1).join(", ") + " " + conj + " " + names[names.length - 1];
}
function whoApproves(names, policy) {
  if (names.length === 1) return names[0] + " approves";
  if (policy === "any") return "One of " + listNames(names, "or") + " approves";
  return listNames(names) + " " + (names.length === 2 ? "both" : "all") + " approve";
}
function fmtWhen(iso) {
  return iso
    ? new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : "";
}
function fmtSize(n) {
  return n >= 1048576
    ? (n / 1048576).toFixed(1) + " MB"
    : Math.max(1, Math.round(n / 1024)) + " KB";
}
function tenantLabel(id) {
  var t = (state.meta?.tenants || []).find(function (x) { return x.id === id; });
  return t ? t.label : id;
}
function chips(list, cls) {
  return '<div class="chips">' + list.map(function (x) { return '<span class="chip ' + cls + '">' + esc(x) + "</span>"; }).join("") + "</div>";
}
function note(el, cls, text) { el.className = "note " + cls; el.textContent = text; }
function selectedTenants() {
  return [].slice.call(document.querySelectorAll("input[name=tenant]:checked")).map(function (c) { return c.value; });
}

// ---------------------------------------------------------------------------
// Validation (the backend re-checks all of it)
// ---------------------------------------------------------------------------
function validGlobalId(v) { return v.trim().replace(/[–—]/g, "-").length > 0; }
function validEmail(v) {
  var m = /^[^\s@]+@([^\s@]+\.[^\s@]+)$/.exec(v.trim());
  if (!m || !state.meta) return false;
  var domain = m[1].toLowerCase(), allowed = state.meta.email_domains;
  return !allowed.length || allowed.some(function (d) { return domain === d || domain.endsWith("." + d); });
}
function normCountry(v) {
  var c = v.trim().toUpperCase();
  return /^[A-Z]{2}$/.test(c) || c === "GLOBAL" || (state.meta?.extra_country_codes || []).includes(c) ? c : null;
}
var FIELD_CHECKS = {
  globalId: function () { return validGlobalId($("globalId").value) ? "" : "Enter a Global ID."; },
  email: function () { return validEmail($("email").value) ? "" : "Enter a Lilly email address" + (state.meta?.email_domains?.length ? " ending @" + state.meta.email_domains[0] : "") + "."; },
  country: function () { return normCountry($("country").value) ? "" : "Enter a two-letter country code, like US, or GLOBAL."; },
};
function setFieldMsg(id, msg) {
  var el = $(id + "Msg");
  el.textContent = msg;
  el.setAttribute("role", msg ? "alert" : "");
  $(id).setAttribute("aria-invalid", msg ? "true" : "false");
}
function checkField(id) { if ($(id).value.trim()) setFieldMsg(id, FIELD_CHECKS[id]()); }

// ---------------------------------------------------------------------------
// In-app notifications (standard live/local version; demo overrides)
// ---------------------------------------------------------------------------
function loadNotifications() {
  var seq = _nextFetchSeq("notif");
  Backend.get("/api/notifications").then(function (data) {
    if (_isStaleFetch("notif", seq)) return;
    var badge = $("notifBadge");
    if (data.unread > 0) {
      badge.textContent = data.unread > 99 ? "99+" : String(data.unread);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    renderNotifList(data.items);
  }).catch(function () { /* silent — poll will retry */ });
}

function renderNotifList(items) {
  var list = $("notifList");
  if (!items.length) {
    list.innerHTML = '<div class="notif-empty">No notifications yet.</div>';
    return;
  }
  list.innerHTML = items.map(function (n) {
    return '<div class="notif-item' + (n.read ? "" : " unread") + '" data-link="' + esc(n.link) + '" data-rid="' + esc(n.request_id) + '">' +
      '<div class="notif-title">' + esc(n.title) + "</div>" +
      '<div class="notif-body">' + esc(n.body) + "</div>" +
      '<div class="notif-time">' + esc(fmtWhen(n.created_at)) + "</div></div>";
  }).join("");
  list.querySelectorAll(".notif-item[data-rid]").forEach(function (el) {
    el.onclick = function () {
      var rid = el.dataset.rid;
      if (rid) { $("notifPanel").classList.remove("show"); openDetail(rid); }
    };
  });
}

function toggleNotifPanel() {
  $("notifPanel").classList.toggle("show");
}

function wireNotifications() {
  $("notifBell").onclick = toggleNotifPanel;
  $("markAllRead").onclick = function () {
    Backend.post("/api/notifications/read-all", {}).then(function () {
      $("notifBadge").classList.add("hidden");
      document.querySelectorAll(".notif-item.unread").forEach(function (el) { el.classList.remove("unread"); });
    }).catch(function () { /* silent */ });
  };
  document.addEventListener("click", function (e) {
    var wrap = $("notifWrap");
    if (wrap && !wrap.contains(e.target)) $("notifPanel").classList.remove("show");
  });
  notifTimer = setInterval(loadNotifications, 60000);
  loadNotifications();
}

// ---------------------------------------------------------------------------
// enterApp — shared entry point after sign-in
// ---------------------------------------------------------------------------
function enterApp() {
  var seq = ++enterSeq;
  return Promise.all([Backend.get("/api/me"), Backend.get("/api/metadata")]).then(function (results) {
    if (seq !== enterSeq) return; // a newer sign-in took over
    var me = results[0], meta = results[1];
    state.me = me;
    state.meta = meta;
    $("gate").classList.add("hidden");
    $("gateError").classList.add("hidden");
    $("app").classList.remove("hidden");
    $("whoBox").classList.remove("hidden");
    $("whoName").textContent = state.me.name || state.me.upn;
    $("approvalsTab").classList.toggle("hidden", !state.me.is_approver);
    renderMeta();
    if (!state.wired) { wire(); wireNotifications(); state.wired = true; }
    resetForm();
    var deep = /^#request-(.+)$/.exec(location.hash);
    if (deep) openDetail(decodeURIComponent(deep[1]), state.me.is_approver ? "approvals" : "mine");
    else showView(state.me.is_approver ? "approvals" : "new");
    refreshApprovalsCount();
  });
}

// ---------------------------------------------------------------------------
// Metadata rendering
// ---------------------------------------------------------------------------
function renderMeta() {
  var m = state.meta;
  $("tenantList").innerHTML = m.tenants.map(function (t) {
    return '<label class="check"><input type="checkbox" name="tenant" value="' + esc(t.id) + '" />' +
      "<span><span class=\"label\">" + esc(t.label) + "</span>" +
      (t.label !== t.id ? ' <span class="mono sub">' + esc(t.id) + "</span>" : "") +
      "</span></label>";
  }).join("") || '<p class="empty">No tenants are open for requests.</p>';
  document.querySelectorAll("input[name=tenant]").forEach(function (c) { c.addEventListener("change", refreshReview); });
  $("approversNote").textContent = m.your_approvers.length
    ? whoApproves(m.your_approvers, m.approval_policy) + " before access is granted."
    : "No approvers are set up for your requests yet, so they can't be submitted.";
  $("fileInput").accept = m.attachment.extensions.join(",");
  $("dropRules").textContent = "Outlook email (.msg or .eml), PDF or screenshot, up to " + fmtSize(m.attachment.max_bytes);
}

// ---------------------------------------------------------------------------
// Wire up event handlers
// ---------------------------------------------------------------------------
function wire() {
  document.querySelectorAll(".tabs [data-view]").forEach(function (b) {
    b.addEventListener("click", function () { showView(b.dataset.view); });
  });
  ["globalId", "email", "country"].forEach(function (id) {
    $(id).addEventListener("input", function () {
      var el = $(id);
      var fixed = el.value.replace(/[–—]/g, "-");
      if (fixed !== el.value) {
        var pos = el.selectionStart - (el.value.length - fixed.length);
        el.value = fixed;
        el.selectionStart = el.selectionEnd = pos;
      }
      setFieldMsg(id, "");
      refreshReview();
    });
    $(id).addEventListener("blur", function () { checkField(id); });
  });
  $("justification").addEventListener("input", refreshReview);
  $("country").addEventListener("input", function () {
    if (state.resolved?.country_scoped) invalidateCheck();
    if ($("dou").value.trim() && !state.resolved) scheduleCheck();
  });
  $("country").addEventListener("change", function () { if ($("dou").value.trim() && !state.resolved) resolveDou(); });
  $("dou").addEventListener("input", function () { invalidateCheck(); if ($("dou").value.trim()) scheduleCheck(); });
  $("dou").addEventListener("change", function () { if (!state.resolved) resolveDou(); });
  $("resolveBtn").onclick = function () { resolveDou(); };
  $("fileInput").addEventListener("change", function (e) { setFile(e.target.files[0] || null); });
  ["dragenter", "dragover"].forEach(function (ev) { $("drop").addEventListener(ev, function () { $("drop").classList.add("over"); }); });
  ["dragleave", "drop"].forEach(function (ev) { $("drop").addEventListener(ev, function () { $("drop").classList.remove("over"); }); });
  $("fileRemove").onclick = function () { setFile(null); };
  $("submitBtn").onclick = submit;
  $("viewMine").onclick = function () { showView("mine"); };
  $("startAnother").onclick = function () { resetForm(); showView("new"); };
  $("backBtn").onclick = function () { showView(state.returnView); };
}

// ---------------------------------------------------------------------------
// View switching (with fade transition)
// ---------------------------------------------------------------------------
function showView(name) {
  state.view = name;
  ["new", "mine", "approvals", "detail"].forEach(function (v) {
    var el = $("view-" + v);
    if (v === name) {
      el.classList.remove("hidden");
      el.style.animation = "fadeInUp 0.35s cubic-bezier(0.4,0,0.2,1) both";
    } else {
      el.classList.add("hidden");
      el.style.animation = "";
    }
  });
  var tab = name === "detail" ? state.returnView : name;
  document.querySelectorAll(".tabs [data-view]").forEach(function (b) {
    var sel = b.dataset.view === tab;
    if (sel) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    b.setAttribute("aria-selected", String(sel));
    b.setAttribute("tabindex", sel ? "0" : "-1");
  });
  if (name !== "detail" && Backend.mode === "live" && location.hash) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  clearInterval(approvalRefreshTimer);
  approvalRefreshTimer = null;
  if (name === "mine") loadMine();
  if (name === "approvals") {
    loadApprovals();
    approvalRefreshTimer = setInterval(loadApprovals, 60000);
  }
}

// ---------------------------------------------------------------------------
// DoU resolution
// ---------------------------------------------------------------------------
var checkTimer = null;
function invalidateCheck() { state.resolved = null; $("derived").classList.add("hidden"); refreshReview(); }
function scheduleCheck() { clearTimeout(checkTimer); checkTimer = setTimeout(resolveDou, 600); }

function resolveDou() {
  clearTimeout(checkTimer);
  var dou = $("dou").value.trim();
  if (!dou) return;
  var country = $("country").value.trim();
  var key = dou + "|" + country;
  if (key === state.checkingKey) return;
  state.checkingKey = key;
  state.resolved = null;
  refreshReview();
  var box = $("derived");
  box.classList.remove("hidden");
  box.innerHTML = '<p class="hint">Checking…</p>';
  var seq = _nextFetchSeq("dou");
  var q = "dou=" + encodeURIComponent(dou) + (country ? "&country=" + encodeURIComponent(country) : "");
  Backend.get("/api/resolve-dou?" + q).then(function (r) {
    if (_isStaleFetch("dou", seq)) return;
    if ($("dou").value.trim() !== dou || $("country").value.trim() !== country) return;
    state.resolved = r;
    renderDerived(r);
  }).catch(function (e) {
    if (_isStaleFetch("dou", seq)) return;
    box.innerHTML = '<div class="banner err">' + esc(e.message) + "</div>";
  }).finally(function () {
    if (state.checkingKey === key) state.checkingKey = null;
    refreshReview();
  });
}

// ---------------------------------------------------------------------------
// Derived DoU rendering (overridable by demo)
// ---------------------------------------------------------------------------
function renderDerived(r) {
  var box = $("derived");
  if (!r.matched) { box.innerHTML = '<div class="banner err">' + esc(r.error) + "</div>"; return; }
  var html = '<div class="uc">' + esc(r.use_case) + "</div>";
  if (r.error) html += '<div class="banner warn">' + esc(r.error) + "</div>";
  if (r.retired) html += '<div class="banner err">This use case is retired, so it can\'t be requested.</div>';
  if (r.unknown_roles.length) {
    html += r.country_scoped
      ? '<div class="banner warn">Reltio doesn\'t have ' + esc(listNames(r.unknown_roles)) + ". Check the country code.</div>"
      : '<div class="banner warn">Reltio doesn\'t recognize ' + esc(listNames(r.unknown_roles)) + ", so this DoU can't be requested until the roles matrix is fixed.</div>";
  }
  if (!r.error && !r.retired && !r.unknown_roles.length && !r.reltio_roles.length)
    html += '<div class="banner warn">This use case grants no Reltio roles. Its access is ECDP-only, so there\'s nothing to request here.</div>';
  if (r.reltio_roles.length)
    html += '<div class="group"><h4>' + (r.grantable ? "Reltio roles you'll get" : "Reltio roles in this use case") + " (" + r.reltio_roles.length + ")</h4>" + chips(r.reltio_roles, "grant") + "</div>";
  if (r.unknown_roles.length)
    html += '<div class="group"><h4>Not in Reltio</h4>' + chips(r.unknown_roles, "unknown") + "</div>";
  if (r.ecdp_roles.length)
    html += '<div class="group"><h4>ECDP roles, granted separately (not by this request)</h4>' + chips(r.ecdp_roles, "ecdp") + "</div>";
  box.innerHTML = html;
}

// ---------------------------------------------------------------------------
// File attachment
// ---------------------------------------------------------------------------
function setFile(file) {
  $("fileMsg").textContent = "";
  if (file) {
    var ext = (/\.[^.]+$/.exec(file.name) || [""])[0].toLowerCase();
    if (!state.meta.attachment.extensions.includes(ext)) {
      $("fileMsg").textContent = "That file type isn't accepted. Attach the email as .msg or .eml, or as a PDF or image.";
      return;
    }
    if (!file.size) { $("fileMsg").textContent = "That file is empty."; return; }
    if (file.size > state.meta.attachment.max_bytes) {
      $("fileMsg").textContent = "That file is " + fmtSize(file.size) + ". The limit is " + fmtSize(state.meta.attachment.max_bytes) + ".";
      return;
    }
  }
  state.file = file;
  state.uploadedKey = null;
  $("fileInput").value = "";
  $("drop").classList.toggle("hidden", !!file);
  $("fileBox").classList.toggle("hidden", !file);
  if (file) { $("fileName").textContent = file.name; $("fileSize").textContent = fmtSize(file.size); }
  refreshReview();
}

// ---------------------------------------------------------------------------
// Form validation
// ---------------------------------------------------------------------------
function missingItems() {
  var m = [];
  if (!validGlobalId($("globalId").value)) m.push("Global ID");
  if (!validEmail($("email").value)) m.push("email");
  if (!normCountry($("country").value)) m.push("country code");
  if (!$("justification").value.trim()) m.push("business justification");
  if (!state.resolved || !state.resolved.grantable) m.push("a DoU that checks out");
  if (!selectedTenants().length) m.push("a tenant");
  if (!state.file) m.push("your manager's approval email");
  return m;
}

// ---------------------------------------------------------------------------
// Bug fix: debounced refreshReview (150ms)
// ---------------------------------------------------------------------------
var _refreshReviewTimer = null;
function refreshReview() {
  clearTimeout(_refreshReviewTimer);
  _refreshReviewTimer = setTimeout(refreshReviewNow, 150);
}

// The core review rendering — overridable by demo
function refreshReviewNow() {
  if (!state.meta) return;
  var r = state.resolved;
  var roles = r && r.grantable ? r.reltio_roles : [];
  $("review").innerHTML =
    "<dt>Global ID</dt><dd class=\"mono\">" + (esc($("globalId").value.trim().toUpperCase()) || "—") + "</dd>" +
    "<dt>Email</dt><dd>" + (esc($("email").value.trim()) || "—") + "</dd>" +
    "<dt>Country</dt><dd>" + (esc(normCountry($("country").value) || "") || "—") + "</dd>" +
    "<dt>Business justification</dt><dd>" + (esc($("justification").value.trim()) || "—") + "</dd>" +
    "<dt>Use case</dt><dd>" + (r && r.matched ? esc(r.use_case) : "—") + "</dd>" +
    "<dt>Reltio roles</dt><dd>" + (esc(roles.join(", ")) || "—") + "</dd>" +
    "<dt>Tenants</dt><dd>" + (esc(selectedTenants().map(tenantLabel).join(", ")) || "—") + "</dd>" +
    "<dt>Manager approval</dt><dd>" + (state.file ? esc(state.file.name) : "—") + "</dd>";
  var missing = missingItems();
  var ready = !missing.length && state.meta.your_approvers.length > 0;
  $("submitBtn").disabled = !ready;
  $("submitHint").textContent = missing.length ? "Still needed: " + listNames(missing) + "." : "";
}

// ---------------------------------------------------------------------------
// Submit request (overridable by demo for email toast)
// ---------------------------------------------------------------------------
function submit() {
  var btn = $("submitBtn"), out = $("submitResult");
  btn.disabled = true;
  out.className = "note hidden";
  var uploadP;
  if (!state.uploadedKey) {
    btn.textContent = "Uploading the attachment…";
    uploadP = Backend.upload(state.file).then(function (key) { state.uploadedKey = key; });
  } else {
    uploadP = Promise.resolve();
  }
  uploadP.then(function () {
    btn.textContent = "Submitting…";
    return Backend.post("/api/requests", {
      global_id: $("globalId").value.trim().replace(/[–—]/g, "-"),
      email: $("email").value.trim(),
      country_code: $("country").value.trim(),
      justification: $("justification").value.trim(),
      dou: $("dou").value.trim(),
      tenants: selectedTenants(),
      attachment_key: state.uploadedKey,
    });
  }).then(function (v) {
    var names = v.required_approvers.map(function (a) { return a.name; });
    var when = v.policy === "any" || names.length === 1 ? "it's approved" : names.length === 2 ? "both have approved" : "everyone has approved";
    $("newDoneText").textContent = "Request " + v.request_id + " is with " + listNames(names, v.policy === "any" ? "or" : "and") + ". The access is granted in Reltio as soon as " + when + ". You can follow it in My requests.";
    $("newForm").classList.add("hidden");
    $("newDone").classList.remove("hidden");
    if (typeof onSubmitDone === "function") onSubmitDone(v);
  }).catch(function (e) {
    note(out, "err", e.message);
  }).finally(function () {
    btn.textContent = "Submit request";
    refreshReview();
  });
}

// ---------------------------------------------------------------------------
// Reset form
// ---------------------------------------------------------------------------
function resetForm() {
  ["globalId", "email", "country", "dou"].forEach(function (id) { $(id).value = ""; });
  $("justification").value = "";
  ["globalId", "email", "country"].forEach(function (id) { setFieldMsg(id, ""); });
  document.querySelectorAll("input[name=tenant]").forEach(function (c) { c.checked = false; });
  state.resolved = null;
  $("derived").classList.add("hidden");
  $("derived").innerHTML = "";
  setFile(null);
  $("submitResult").className = "note hidden";
  $("newForm").classList.remove("hidden");
  $("newDone").classList.add("hidden");
  refreshReview();
}

// ---------------------------------------------------------------------------
// Request list helpers
// ---------------------------------------------------------------------------
function progressText(v) {
  if (v.status !== "PENDING") return (STATUS[v.status] || v.status) + ", " + fmtWhen(v.updated_at);
  var approved = v.required_approvers.filter(function (a) { return a.decision?.decision === "approve"; }).map(function (a) { return a.name; });
  var waiting = v.required_approvers.filter(function (a) { return !a.decision; }).map(function (a) { return a.name; });
  return (approved.length ? "Approved by " + listNames(approved) + "; " : "") + "waiting on " + listNames(waiting, v.policy === "any" ? "or" : "and");
}

function row(v, from) {
  var who = from === "approvals" ? esc(v.requester.name) + " for " : "";
  return '<button type="button" class="req" data-id="' + esc(v.request_id) + '">' +
    '<span class="req-head"><span class="req-title">' + esc(v.use_case) + '</span><span class="pill ' + esc(v.status) + '">' + pillIcon(v.status) + esc(STATUS[v.status] || v.status) + "</span></span>" +
    '<span class="req-meta">' + who + '<span class="mono">' + esc(v.global_id) + "</span> on " + esc(v.tenants.map(function (t) { return t.label; }).join(", ")) + "</span>" +
    '<span class="req-meta">' + esc(progressText(v)) + "</span></button>";
}
function bindRows(box, from) {
  box.querySelectorAll(".req").forEach(function (b, i) {
    b.style.animationDelay = (i * 0.06) + "s";
    b.addEventListener("click", function () { openDetail(b.dataset.id, from); });
  });
}

// ---------------------------------------------------------------------------
// My Requests list (overridable by demo for simplified version)
// ---------------------------------------------------------------------------
var minePageSize = 50;
function loadMine() {
  var box = $("mineList");
  box.innerHTML = shimmerSkeleton(3);
  var seq = _nextFetchSeq("mine");
  Backend.get("/api/requests?limit=" + minePageSize).then(function (items) {
    if (_isStaleFetch("mine", seq)) return;
    if (!items.length) { box.innerHTML = '<p class="empty">You haven\'t requested any access yet.</p>'; return; }
    box.innerHTML = items.map(function (v) { return row(v, "mine"); }).join("");
    bindRows(box, "mine");
    if (items.length >= minePageSize) {
      var btn = document.createElement("button");
      btn.className = "link";
      btn.textContent = "Load more";
      btn.style.cssText = "display:block;margin:1rem auto";
      btn.onclick = function () { minePageSize += 50; loadMine(); };
      box.appendChild(btn);
    }
  }).catch(function (e) {
    if (_isStaleFetch("mine", seq)) return;
    box.innerHTML = '<div class="note err">' + esc(e.message) + "</div>";
  });
}

// ---------------------------------------------------------------------------
// Approvals list (overridable by demo for simplified version)
// ---------------------------------------------------------------------------
var approvalsPageSize = 30;
function loadApprovals() {
  var w = $("waitingList"), r = $("recentList");
  w.innerHTML = shimmerSkeleton(2);
  r.innerHTML = shimmerSkeleton(2);
  var seq = _nextFetchSeq("approvals");
  Backend.get("/api/approvals?limit=" + approvalsPageSize).then(function (data) {
    if (_isStaleFetch("approvals", seq)) return;
    w.innerHTML = data.waiting.length ? data.waiting.map(function (v) { return row(v, "approvals"); }).join("") : '<p class="empty">Nothing is waiting on you.</p>';
    r.innerHTML = data.recent.length ? data.recent.map(function (v) { return row(v, "approvals"); }).join("") : '<p class="empty">No other requests yet.</p>';
    bindRows(w, "approvals");
    bindRows(r, "approvals");
    setApprovalsCount(data.waiting.length);
    if (data.recent.length >= approvalsPageSize) {
      var btn = document.createElement("button");
      btn.className = "link";
      btn.textContent = "Load more";
      btn.style.cssText = "display:block;margin:1rem auto";
      btn.onclick = function () { approvalsPageSize += 30; loadApprovals(); };
      r.appendChild(btn);
    }
  }).catch(function (e) {
    if (_isStaleFetch("approvals", seq)) return;
    w.innerHTML = '<div class="note err">' + esc(e.message) + "</div>";
    r.innerHTML = "";
  });
}

function refreshApprovalsCount() {
  if (!state.me?.is_approver) return;
  Backend.get("/api/approvals").then(function (data) {
    setApprovalsCount(data.waiting.length);
  }).catch(function () { /* badge only */ });
}
function setApprovalsCount(n) {
  var el = $("approvalsCount");
  el.textContent = n;
  el.classList.toggle("hidden", !n);
}

// ---------------------------------------------------------------------------
// Request detail
// ---------------------------------------------------------------------------
function openDetail(id, from) {
  state.returnView = from || state.view;
  decisionDraft = null;
  showView("detail");
  if (Backend.mode === "live") history.replaceState(null, "", "#request-" + encodeURIComponent(id));
  $("backBtn").textContent = state.returnView === "approvals" ? "Back to approvals" : "Back to my requests";
  $("detailBody").innerHTML = shimmerSkeleton(4);
  var seq = _nextFetchSeq("detail");
  Backend.get("/api/requests/" + encodeURIComponent(id)).then(function (v) {
    if (_isStaleFetch("detail", seq)) return;
    renderDetail(v);
  }).catch(function (e) {
    if (_isStaleFetch("detail", seq)) return;
    $("detailBody").innerHTML = '<div class="note err">' + esc(e.message) + "</div>";
  });
}

function resultHtml(v) {
  var r = v.result || {};
  var fmt = function (map) {
    return Object.entries(map || {}).map(function (pair) { return pair[0] + " on " + pair[1].map(tenantLabel).join(", "); }).join("; ");
  };
  var lines = [];
  if (r.action === "created") lines.push("Created " + v.global_id + " in Reltio with " + v.reltio_roles.length + " role" + (v.reltio_roles.length === 1 ? "" : "s") + ".");
  if (r.action === "updated") lines.push("Added to " + v.global_id + ": " + fmt(r.added) + ".");
  if (r.action === "no_change") lines.push(v.global_id + " already had all of this access, so nothing changed.");
  if (r.action !== "no_change" && Object.keys(r.already_had || {}).length) lines.push("Already had: " + fmt(r.already_had) + ".");
  if (v.ecdp_roles.length) lines.push("ECDP roles still need granting separately: " + v.ecdp_roles.join(", ") + ".");
  return lines.map(function (l) { return '<span class="detail-text">' + esc(l) + "</span>"; }).join("") +
    (r.warnings || []).map(function (w) { return '<span class="detail-text warn-text">' + esc(w) + "</span>"; }).join("");
}

function timeline(v) {
  var items = ['<li class="done">Submitted by ' + esc(v.requester.name) + '<span class="when">' + esc(fmtWhen(v.created_at)) + "</span></li>"];
  var closedEarly = v.status === "REJECTED" || v.status === "CANCELLED" || (v.policy === "any" && v.status !== "PENDING");
  v.required_approvers.forEach(function (a) {
    var d = a.decision;
    if (d?.decision === "approve")
      items.push('<li class="done">' + esc(a.name) + " approved" + '<span class="when">' + esc(fmtWhen(d.at)) + "</span>" + (d.comment ? '<span class="detail-text">"' + esc(d.comment) + '"</span>' : "") + "</li>");
    else if (d?.decision === "reject")
      items.push('<li class="bad">' + esc(a.name) + " rejected" + '<span class="when">' + esc(fmtWhen(d.at)) + '</span><span class="detail-text">"' + esc(d.comment) + '"</span></li>');
    else if (closedEarly)
      items.push('<li class="skip">' + esc(a.name) + '<span class="when">Not needed</span></li>');
    else
      items.push('<li class="wait">' + esc(a.name) + '<span class="when">Waiting for approval</span></li>');
  });
  if (v.status === "CANCELLED") items.push('<li class="skip">Cancelled by the requester<span class="when">' + esc(fmtWhen(v.updated_at)) + "</span></li>");
  else if (v.status === "REJECTED") items.push('<li class="bad">Closed<span class="when">No access was granted</span></li>');
  else if (v.status === "PROVISIONING") items.push('<li class="wait">Granting access in Reltio…</li>');
  else if (v.status === "FAILED") items.push('<li class="bad">Granting access failed<span class="when">' + esc(fmtWhen(v.updated_at)) + '</span><span class="detail-text">' + esc(v.error) + "</span></li>");
  else if (v.status === "COMPLETED") items.push('<li class="done">Access granted<span class="when">' + esc(fmtWhen(v.updated_at)) + "</span>" + resultHtml(v) + "</li>");
  else items.push('<li class="todo">Access granted<span class="when">After approval</span></li>');
  return '<ol class="timeline">' + items.join("") + "</ol>";
}

function actionsHtml(v) {
  var f = v.viewer;
  if (f.can_decide) {
    if (!decisionDraft) decisionDraft = { step: "edit", roles: new Set(v.role_detail || []) };
    var allRoles = v.role_detail || [];
    if (decisionDraft.step === "confirm") {
      var kept = allRoles.filter(function (r) { return decisionDraft.roles.has(r); });
      return '<div class="decision">' +
        '<h3 class="section-title">Confirm access to grant</h3>' +
        "<p>You're approving <strong>" + kept.length + "</strong> access item" + (kept.length === 1 ? "" : "s") + ".</p>" +
        "<ul>" + kept.map(function (r) { return '<li class="mono">' + esc(r) + "</li>"; }).join("") + "</ul>" +
        (decisionDraft.comment ? "<p><strong>Comment:</strong> " + esc(decisionDraft.comment) + "</p>" : "") +
        '<div class="actions">' +
        '<button type="button" class="primary" id="confirmApproveBtn">Confirm and grant access</button>' +
        '<button type="button" id="backToEditBtn">Back</button></div></div>';
    }
    return '<div class="decision">' +
      '<h3 class="section-title">Your decision</h3>' +
      "<p>Uncheck any access items you don't want to grant:</p>" +
      '<div class="checks">' + allRoles.map(function (r) {
        return '<label class="check"><input type="checkbox" name="approveRole" value="' + esc(r) + '" ' + (decisionDraft.roles.has(r) ? "checked" : "") + " />" +
          '<span class="label mono">' + esc(r) + "</span></label>";
      }).join("") + "</div>" +
      '<label class="field-label">Comment (optional)<textarea id="decisionComment" rows="2" maxlength="1000" placeholder="Any notes for the requester?">' + esc(decisionDraft.comment || "") + "</textarea></label>" +
      '<div class="actions">' +
      '<button type="button" class="primary" id="approveBtn">Continue</button>' +
      '<button type="button" class="danger" id="rejectBtn">Reject</button></div></div>';
  }
  if (f.can_retry) return '<div class="actions"><button type="button" class="primary" id="retryBtn">Try granting access again</button></div>';
  if (f.can_cancel) return '<div class="actions"><button type="button" class="danger" id="cancelBtn">Cancel request</button></div>';
  return "";
}

function renderDetail(v) {
  $("detailBody").style.animation = "none";
  $("detailBody").offsetHeight;
  $("detailBody").style.animation = "fadeInUp 0.4s cubic-bezier(0.4,0,0.2,1) both";
  $("detailBody").innerHTML =
    '<div class="detail-head">' +
    '<h2 class="detail-title">' + esc(v.use_case) + "</h2>" +
    '<span class="pill ' + esc(v.status) + '">' + pillIcon(v.status) + esc(STATUS[v.status] || v.status) + "</span></div>" +
    '<p class="detail-id">Request <span class="mono">' + esc(v.request_id) + "</span>, submitted " + esc(fmtWhen(v.created_at)) + "</p>" +
    '<dl class="facts">' +
    "<dt>Requested by</dt><dd>" + esc(v.requester.name) + "</dd>" +
    '<dt>Global ID</dt><dd class="mono">' + esc(v.global_id) + "</dd>" +
    "<dt>Email</dt><dd>" + esc(v.email) + "</dd>" +
    "<dt>Country</dt><dd>" + esc(v.country_code) + "</dd>" +
    "<dt>Business justification</dt><dd>" + esc(v.justification || "—") + "</dd>" +
    "<dt>DoU</dt><dd>" + esc(v.dou) + "</dd>" +
    "<dt>Tenants</dt><dd>" + v.tenants.map(function (t) { return esc(t.label) + (t.label !== t.id ? ' <span class="mono sub">' + esc(t.id) + "</span>" : ""); }).join("<br>") + "</dd>" +
    "<dt>Manager approval</dt><dd>" + esc(v.attachment.filename) + ' <span class="sub">' + esc(fmtSize(v.attachment.size)) + '</span> <button type="button" class="link" id="openAttachment">Open</button></dd></dl>' +
    '<div class="group"><h4>Reltio roles (' + v.reltio_roles.length + ")</h4>" + chips(v.reltio_roles, "grant") + "</div>" +
    (v.ecdp_roles.length ? '<div class="group"><h4>ECDP roles, granted separately (not by this request)</h4>' + chips(v.ecdp_roles, "ecdp") + "</div>" : "") +
    '<h3 class="section-title spaced">Progress</h3>' +
    timeline(v) +
    actionsHtml(v) +
    '<div class="note hidden" id="actionResult" aria-live="polite"></div>';
  $("openAttachment").onclick = function () {
    Backend.openAttachment(v.request_id).then(function (msg) {
      if (msg) note($("actionResult"), "info", msg);
    }).catch(function (e) {
      note($("actionResult"), "err", e.message);
    });
  };
  wireActions(v);
}

function wireActions(v) {
  var id = encodeURIComponent(v.request_id);
  var run = function (action, btn, busyLabel, call) {
    var buttons = [].slice.call(document.querySelectorAll("#detailBody .actions button"));
    buttons.forEach(function (b) { b.disabled = true; });
    var label = btn.textContent;
    btn.textContent = busyLabel;
    call().then(function (nv) {
      renderDetail(nv);
      afterAction(nv, action);
    }).catch(function (e) {
      buttons.forEach(function (b) { b.disabled = false; });
      btn.textContent = label;
      note($("actionResult"), "err", e.message);
    });
  };

  document.querySelectorAll("input[name=approveRole]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      if (cb.checked) decisionDraft.roles.add(cb.value);
      else decisionDraft.roles.delete(cb.value);
    });
  });

  var approve = $("approveBtn"), reject = $("rejectBtn"), retry = $("retryBtn"), cancel = $("cancelBtn");
  var confirmApprove = $("confirmApproveBtn"), backToEdit = $("backToEditBtn");

  if (approve) approve.onclick = function () {
    var checked = [].slice.call(document.querySelectorAll("input[name=approveRole]:checked"));
    if (!checked.length) { note($("actionResult"), "err", "Check at least one access item to approve."); return; }
    decisionDraft.comment = ($("decisionComment")?.value || "").trim();
    decisionDraft.step = "confirm";
    renderDetail(v);
  };
  if (backToEdit) backToEdit.onclick = function () {
    decisionDraft.step = "edit";
    renderDetail(v);
  };
  if (confirmApprove) confirmApprove.onclick = function () {
    run("approve", confirmApprove, "Granting access…", function () {
      return Backend.post("/api/requests/" + id + "/decision", { decision: "approve", comment: decisionDraft.comment || "", approved_roles: [].concat([...decisionDraft.roles]) });
    });
  };
  if (reject) reject.onclick = function () {
    var comment = $("decisionComment")?.value?.trim() || "";
    if (!comment) { note($("actionResult"), "err", "Add a comment explaining why you're rejecting it."); if ($("decisionComment")) $("decisionComment").focus(); return; }
    if (!confirm("Reject this request?")) return;
    run("reject", reject, "Rejecting…", function () {
      return Backend.post("/api/requests/" + id + "/decision", { decision: "reject", comment: comment });
    });
  };
  if (retry) retry.onclick = function () {
    run("retry", retry, "Trying again…", function () {
      return Backend.post("/api/requests/" + id + "/retry", {});
    });
  };
  if (cancel) cancel.onclick = function () {
    if (!confirm("Cancel this request?")) return;
    run("cancel", cancel, "Cancelling…", function () {
      return Backend.post("/api/requests/" + id + "/cancel", {});
    });
  };
}

function afterAction(v, action) {
  var waiting = v.required_approvers.filter(function (a) { return !a.decision; }).map(function (a) { return a.name; });
  var messages = {
    approve: { COMPLETED: "Approved. The access has been granted in Reltio.", PENDING: "Approved. Waiting on " + listNames(waiting) + ".", FAILED: "Approved, but Reltio didn't accept the change. The error is in the progress list; you can try again." },
    reject: { REJECTED: "Rejected. The requester can see your comment." },
    retry: { COMPLETED: "The access has been granted in Reltio.", FAILED: "Reltio still didn't accept the change. The error is in the progress list." },
    cancel: { CANCELLED: "Request cancelled." },
  };
  var msg = messages[action]?.[v.status];
  if (msg) note($("actionResult"), v.status === "FAILED" ? "err" : "ok", msg);
  refreshApprovalsCount();
  loadNotifications();
}
