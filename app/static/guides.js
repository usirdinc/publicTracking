(() => {
  if (typeof HTMLDialogElement === 'undefined') return;
  document.querySelectorAll('[data-open-guide]').forEach(button => { button.hidden = false; });
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-open-guide]');
    if (opener) {
      const dialog = document.getElementById(opener.dataset.openGuide);
      if (dialog && !dialog.open) dialog.showModal();
    }
    const close = event.target.closest('[data-close-guide]');
    if (close) close.closest('dialog').close();
  });
  // Case cards are replaced after checks. New help controls stay available.
  const cases = document.querySelector('.cases');
  if (cases) new MutationObserver(() => {
    cases.querySelectorAll('[data-open-guide]').forEach(button => { button.hidden = false; });
  }).observe(cases, { childList: true, subtree: true });
})();
