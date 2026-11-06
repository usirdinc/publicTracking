// USIRD Case Tracker front end: confirms, relative times, and the live check progress.
(function () {
  'use strict';

  document.querySelectorAll('[data-password-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var input = document.getElementById(button.getAttribute('aria-controls'));
      if (!input) return;
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.textContent = show ? 'Hide' : 'Show';
      button.setAttribute('aria-pressed', String(show));
    });
  });
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-dismiss-notice]');
    if (button) button.closest('.toast').remove();
  });


  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // ---- relative times ("5 min ago", "tomorrow")
  function rel(iso) {
    var t = Date.parse(iso); if (isNaN(t)) return null;
    var s = (t - Date.now()) / 1000, a = Math.abs(s), past = s < 0;
    if (a < 60) return past ? 'just now' : 'in a moment';
    var m = Math.round(a / 60), h = Math.round(a / 3600), d = Math.round(a / 86400);
    if (a < 3600) return past ? m + ' min ago' : 'in ' + m + ' min';
    if (a < 86400 * 0.9) return past ? h + (h === 1 ? ' hour ago' : ' hours ago') : 'in ' + h + (h === 1 ? ' hour' : ' hours');
    if (d === 1) return past ? 'yesterday' : 'tomorrow';
    return past ? d + ' days ago' : 'in ' + d + ' days';
  }
  function paintTimes(root) {
    (root || document).querySelectorAll('time[data-rel]').forEach(function (el) {
      var r = rel(el.getAttribute('datetime')); if (r) el.textContent = r;
    });
  }
  function paintDates(root) {
    (root || document).querySelectorAll('time[data-date]').forEach(function (el) {
      var v = el.getAttribute('datetime') || '', t = Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(v) ? v + 'T12:00:00' : v);
      if (!isNaN(t)) el.textContent = new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    });
  }
  paintTimes(); paintDates();
  setInterval(paintTimes, 30000);

  // ---- subscribe page: after the payment tab opens, wait for the webhook to turn the account on
  var sub = document.querySelector('[data-subscribe]');
  if (sub) {
    var waitBox = sub.querySelector('[data-waiting]'), waiting = false;
    var check = function () {
      fetch('/account/paid', { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); })
        .then(function (d) { if (d.paid) location.href = '/app'; else setTimeout(check, 4000); })
        .catch(function () { setTimeout(check, 8000); });
    };
    var startWaiting = function () { if (waiting) return; waiting = true; if (waitBox) waitBox.hidden = false; };
    var pay = sub.querySelector('[data-pay]');
    if (pay) pay.addEventListener('click', startWaiting);
    // Also confirm on page load: Square can return before its signed webhook arrives.
    check();
  }

  // ---- progress stepper (shared by the dashboard and the landing page demo)
  var STOPS = [0, 25, 45, 75, 90];
  function stepperAt(p) {           // 0..1 along the line, step to step
    for (var i = STOPS.length - 1; i >= 0; i--) {
      if (p >= STOPS[i]) {
        if (i === STOPS.length - 1) return 1;
        return (i + (p - STOPS[i]) / (STOPS[i + 1] - STOPS[i])) / (STOPS.length - 1);
      }
    }
    return 0;
  }
  function paintStepper(root, p, complete) {
    var st = root.querySelector('[data-stepper]'); if (!st) return;
    var f = stepperAt(p);
    st.querySelector('[data-fill]').style.transform = 'scaleX(' + f.toFixed(4) + ')';
    st.querySelector('[data-dot]').style.left = (f * 100).toFixed(2) + '%';
    var items = st.querySelectorAll('li'), active = 0;
    items.forEach(function (li, i) { if (p >= Number(li.dataset.at)) active = i; });
    items.forEach(function (li, i) {
      var done = complete || i < active;
      li.classList.toggle('done', done);
      li.classList.toggle('active', !done && i === active);
    });
  }

  // ---- live progress
  if (!document.querySelector('[data-case]')) return;
  var MILESTONES = [5, 10, 25, 45, 60, 75, 90, 100];
  var live = {};   // case id -> {shown, target, stage, finishing}
  var polling = false, timer = null, raf = null;

  function card(id) { return document.querySelector('[data-case="' + id + '"]'); }
  function nextMilestone(p) { for (var i = 0; i < MILESTONES.length; i++) if (MILESTONES[i] > p) return MILESTONES[i]; return 100; }

  function begin(id, pct, stage) {
    var el = card(id); if (!el) return;
    if (!live[id]) live[id] = { shown: 0, target: 0, stage: '', finishing: false };
    var L = live[id];
    L.target = Math.max(L.target, pct || 2);
    if (stage) L.stage = stage;
    el.classList.add('busy'); el.classList.remove('done');
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function finish(id) {
    var L = live[id]; if (!L || L.finishing) return;
    L.finishing = true; L.target = 100; L.stage = 'Done';
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function render(id) {
    var el = card(id), L = live[id]; if (!el || !L) return;
    var p = Math.min(100, L.shown);
    el.querySelector('[data-pct]').textContent = Math.round(p) + '%';
    el.querySelector('[data-stage]').textContent = L.stage || 'Checking with USCIS';
    paintStepper(el, p, p >= 99.9);
  }

  function tick() {
    raf = null;
    var active = false;
    Object.keys(live).forEach(function (id) {
      var L = live[id];
      if (L.shown < L.target) {
        // ease toward the step the worker reported
        L.shown += Math.max(0.25, (L.target - L.shown) * (L.finishing ? 0.12 : 0.07));
        if (L.shown > L.target) L.shown = L.target;
      } else if (!L.finishing) {
        // creep slowly toward the next step so the bar never looks frozen
        var cap = nextMilestone(L.target) - 1.5;
        L.shown += (cap - L.shown) * 0.006;
      }
      render(id);
      if (L.finishing && L.shown >= 99.9) { if (!L.revealing) { L.revealing = true; reveal(id); } }
      else active = true;
    });
    if (active) raf = requestAnimationFrame(tick);
  }

  // Swap in the fresh case card from the server with a soft animation.
  function reveal(id) {
    var el = card(id); if (!el) return;
    el.classList.add('done');
    setTimeout(function () {
      fetch('/app', { credentials: 'same-origin' }).then(function (r) { return r.text(); }).then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.querySelector('[data-case="' + id + '"]');
        delete live[id];
        if (!fresh) { location.reload(); return; }
        fresh.classList.add('reveal');
        var selected = el.querySelector('[role="tab"][aria-selected="true"]');
        var focusedTab = el.contains(document.activeElement) && document.activeElement.matches('[data-case-tab]');
        el.replaceWith(fresh);
        wire(fresh, selected && selected.dataset.caseTab);
        if (focusedTab) {
          var activeTab = fresh.querySelector('[role="tab"][aria-selected="true"]');
          if (activeTab) activeTab.focus({ preventScroll: true });
        }
        paintTimes(fresh); paintDates(fresh);
      }).catch(function () { location.reload(); });
    }, 650);
  }

  function poll() {
    timer = null;
    fetch('/cases/status', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      var anyBusy = false;
      (d.cases || []).forEach(function (c) {
        if (c.busy) { anyBusy = true; begin(c.id, c.progress, c.stage); }
        else if (live[c.id]) finish(c.id);
      });
      polling = anyBusy;
      if (anyBusy) timer = setTimeout(poll, 1200);
    }).catch(function () { timer = setTimeout(poll, 4000); });
  }
  function ensurePolling() { if (!timer) timer = setTimeout(poll, 300); }

  function wireCaseTabs(tablist, selected) {
    var tabs = Array.from(tablist.querySelectorAll('[data-case-tab]'));
    var caseCard = tablist.closest('[data-case]');
    var panels = tabs.map(function (tab) {
      return caseCard.querySelector('#' + tab.getAttribute('aria-controls'));
    });
    if (panels.some(function (panel) { return !panel; })) return;

    function activate(index, moveFocus) {
      tabs.forEach(function (tab, i) {
        var active = i === index;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        panels[i].hidden = !active;
      });
      if (moveFocus) tabs[index].focus();
    }

    tabs.forEach(function (tab, i) {
      panels[i].setAttribute('role', 'tabpanel');
      panels[i].setAttribute('aria-labelledby', tab.id);
      panels[i].tabIndex = 0;
      tab.addEventListener('click', function () { activate(i, false); });
      tab.addEventListener('keydown', function (event) {
        var next;
        if (event.key === 'ArrowRight') next = (i + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (i + tabs.length - 1) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault();
        activate(next, true);
      });
    });
    activate(Math.max(0, tabs.findIndex(function (tab) { return tab.dataset.caseTab === selected; })), false);
    tablist.hidden = false;
    caseCard.classList.add('case-tabbed');
  }

  function wire(root, selected) {
    root.querySelectorAll('.case-tablist').forEach(function (tablist) {
      wireCaseTabs(tablist, selected);
    });
    root.querySelectorAll('form[data-check]').forEach(function (f) {
      f.addEventListener('submit', function (e) {
        e.preventDefault();
        var id = f.closest('[data-case]').dataset.case, btn = f.querySelector('button');
        btn.disabled = true;
        begin(id, 2, 'Starting');
        fetch(f.action, { method: 'POST', body: new FormData(f), credentials: 'same-origin',
                          headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            btn.disabled = false;
            if (!d.ok) {
              var el = card(id); if (el) el.classList.remove('busy');
              delete live[id];
              toast(d.message || 'Could not start a check right now.');
            }
            ensurePolling();
          })
          .catch(function () { btn.disabled = false; ensurePolling(); });
      });
    });
    root.querySelectorAll('form[data-confirm]').forEach(function (f) {
      if (f.dataset.wired) return; f.dataset.wired = 1;
      f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
    });
  }

  function toast(msg) {
    var t = document.createElement('div'); t.className = 'toast'; t.setAttribute('role', 'status'); t.textContent = msg;
    document.body.appendChild(t); setTimeout(function () { t.remove(); }, 6500);
  }

  document.querySelectorAll('form[data-confirm]').forEach(function (f) { f.dataset.wired = 1; });
  wire(document);
  poll();
})();
