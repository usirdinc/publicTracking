// Square owns the card iframe. Never log or persist a payment token.
(function () {
  'use strict';
  var form = document.querySelector('[data-square-checkout]');
  var resume = document.querySelector('[data-square-resume]');
  if (!form && !resume) return;
  var target = form || resume;
  var button = target.querySelector('button[type=submit]');
  var message = document.querySelector('[data-payment-message]');
  var busy = false;

  async function send(fields) {
    var response = await fetch('/billing/subscribe', {
      method: 'POST', body: fields, credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json' }
    });
    var result = await response.json();
    if (!response.ok) {
      message.textContent = result.message || 'Payment could not be confirmed. Please try again.';
      return false;
    }
    if (result.state === 'paid') location.assign('/app');
    else location.reload(); // Render the persisted attempt; never create a second subscription.
    return true;
  }

  async function submit(event, card) {
    event.preventDefault();
    if (busy || !target.reportValidity()) return;
    busy = true;
    button.disabled = true;
    message.textContent = 'Please wait while Square confirms your subscription…';
    var submitted = false;
    try {
      var fields = new FormData(target);
      if (card) {
        var name = fields.get('cardholder').trim().split(/\s+/);
        var result = await card.tokenize({
          intent: 'STORE', customerInitiated: true, sellerKeyedIn: false,
          billingContact: { email: form.dataset.email, givenName: name[0], familyName: name.slice(1).join(' ') }
        });
        if (result.status !== 'OK') {
          message.textContent = 'Check your card details and complete any card verification, then try again.';
          return;
        }
        fields.set('source_id', result.token);
      }
      submitted = true;
      await send(fields);
    } catch (error) {
      if (submitted) {
        // A lost browser response can follow a successful charge. Reload the server's
        // persisted state before permitting any further attempt.
        location.reload();
      } else {
        message.textContent = 'The secure card form could not complete verification. Please refresh and try again.';
      }
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  if (resume) {
    resume.addEventListener('submit', function (event) { submit(event, null); });
    return;
  }
  // Prevent an ordinary form POST while the external SDK is still loading.
  form.addEventListener('submit', function (event) { event.preventDefault(); });
  var loading = form.querySelector('[data-card-loading]');
  var script = document.createElement('script');
  script.src = form.dataset.sdkUrl;
  script.async = true;
  script.onerror = function () {
    loading.textContent = 'Square’s secure card fields could not load. Please refresh or allow Square in your browser settings.';
  };
  script.onload = async function () {
    try {
      var payments = window.Square.payments(form.dataset.appId, form.dataset.locationId);
      var card = await payments.card();
      await card.attach('#card-container');
      loading.hidden = true;
      button.disabled = false;
      form.addEventListener('submit', function (event) { submit(event, card); });
    } catch (error) {
      console.warn('Square card initialization:', error.name, error.message);
      loading.textContent = 'Square’s secure card fields are temporarily unavailable. Please refresh and try again.';
    }
  };
  document.head.appendChild(script);
})();
