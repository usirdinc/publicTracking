// Confirm destructive forms, and poll case status while a check is running.
document.querySelectorAll('form[data-confirm]').forEach(function (f) {
  f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
});
(function () {
  if (!document.querySelector('[data-case]')) return;
  var tries = 0;
  function poll() {
    fetch('/cases/status', {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (d) {
      var busy = false;
      (d.cases || []).forEach(function (c) {
        var el = document.querySelector('[data-case="' + c.id + '"]');
        if (!el) return;
        var s = el.querySelector('[data-status]');
        s.textContent = c.text;
        s.className = 'status tone-' + c.tone;
        var pg = el.querySelector('[data-progress]');
        if (pg) {
          pg.hidden = !c.busy;
          if (c.busy) {
            pg.querySelector('.bar span').style.width = (c.progress || 5) + '%';
            pg.querySelector('[data-stage]').textContent = c.stage || '';
          }
        }
        if (c.busy) busy = true;
      });
      if (busy && tries++ < 150) setTimeout(poll, 1500);
      else if (tries > 0 && !busy) location.reload();
    }).catch(function () {});
  }
  poll();
})();
