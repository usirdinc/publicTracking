// Refresh translation blocks only; never replace forms or interrupt case checks.
(function () {
  'use strict';
  if (location.pathname !== '/app') return;
  var timer, running = false, attempts = 0;
  function pending() { return !!document.querySelector('[data-translation-pending="1"]'); }
  function burmese() { return window.USIRDLocale && window.USIRDLocale.language() === 'my'; }
  function schedule() {
    clearTimeout(timer);
    if (burmese() && pending() && attempts < 90) timer = setTimeout(refresh, 4000);
  }
  async function refresh() {
    if (running || !burmese() || document.hidden) { schedule(); return; }
    running = true; attempts += 1;
    try {
      var response = await fetch('/app', {credentials:'same-origin', cache:'no-store'});
      if (!response.ok || new URL(response.url).pathname !== '/app') return;
      var doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      var fresh = new Map();
      doc.querySelectorAll('[data-uscis-source-key]').forEach(function(el) { fresh.set(el.dataset.uscisSourceKey, el); });
      document.querySelectorAll('[data-uscis-source-key]').forEach(function(el) {
        var replacement = fresh.get(el.dataset.uscisSourceKey);
        if (replacement) el.replaceWith(replacement.cloneNode(true));
      });
    } catch (_) { /* Original USCIS text stays readable on network errors. */ }
    finally { running = false; schedule(); }
  }
  document.addEventListener('usird:language', function() { attempts = 0; if (burmese()) refresh(); else clearTimeout(timer); });
  document.addEventListener('visibilitychange', function() { if (!document.hidden) schedule(); });
  var observer = new MutationObserver(function() { if (!running) schedule(); });
  observer.observe(document.getElementById('main'), {childList:true, subtree:true});
  schedule();
})();
