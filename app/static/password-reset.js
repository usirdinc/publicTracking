(function () {
  'use strict';
  var input=document.querySelector('[data-reset-token]');
  if (!input) return;
  var token=new URLSearchParams(location.hash.slice(1)).get('token');
  if (token && /^[A-Za-z0-9_-]{43}$/.test(token)) input.value=token;
  if (location.hash) history.replaceState(null,'',location.pathname);
})();
