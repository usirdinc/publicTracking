(() => {
  const date = document.querySelector('[name="dob"]');
  if (!date) return;

  const form = date.form;
  const now = new Date();
  const year = now.getFullYear();
  const isoDate = (y, m, d) => [y, m, d].map((part, i) =>
    String(part).padStart(i ? 2 : 4, '0')).join('-');
  date.max = isoDate(year, now.getMonth() + 1, now.getDate());

  const rules = {
    first_name: value => !value.trim() && 'Enter the applicant’s first name.',
    last_name: value => !value.trim() && 'Enter the applicant’s last name.',
    email: value => !/^[^@\s]+@(?:[a-z\d](?:[a-z\d-]*[a-z\d])?\.)+[a-z\d-]+$/i.test(value.trim())
      && 'Enter an email address like name@example.com.',
    phone: value => {
      const raw = value.trim();
      let digits = raw.replace(/[^0-9]/g, '');
      if (!raw.startsWith('+') && digits.length === 10) digits = '1' + digits;
      return (!/^\+?[0-9(). -]+$/.test(raw) || !/^[1-9][0-9]{7,14}$/.test(digits)
        || (!raw.startsWith('+') && !/^1[0-9]{10}$/.test(digits))
        || (digits.startsWith('1') && !/^1[2-9][0-9]{2}[2-9][0-9]{6}$/.test(digits)))
        && 'Enter a 10-digit US or Canadian number, or include + and the country code.';
    },
    a_numbers: value => {
      const numbers = value.split(/[;,\s]+/).filter(Boolean);
      if (!numbers.length || numbers.some(number => !/^A?(?:[0-9]{8,9}|[0-9]{2,3}-[0-9]{3}-[0-9]{3})$/i.test(number))) {
        return 'Enter 8 or 9 digits per A-Number, with an optional A prefix.';
      }
      if (new Set(numbers.map(number => number.replace(/[^0-9]/g, '').padStart(9, '0'))).size > 4) {
        return 'Enter up to four different A-Numbers.';
      }
      return '';
    },
    receipt: value => !/^[A-Z]{3}[0-9]{10}$/i.test(value.replace(/[\s-]/g, ''))
      && 'Enter three letters followed by ten digits.',
  };
  const steps = Array.from(form.querySelectorAll('[data-intake-step]'));
  const back = form.querySelector('[data-intake-back]');
  const next = form.querySelector('[data-intake-next]');
  const submit = form.querySelector('[data-intake-submit]');
  const cancel = form.querySelector('[data-intake-cancel]');
  const progress = form.querySelector('[data-intake-progress]');
  const position = progress.querySelector('[data-intake-position]');
  const meter = progress.querySelector('progress');
  const validators = new Map();
  let current = 0;
  const stages = steps.map((step, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = step.dataset.stepLabel;
    button.addEventListener('click', () => {
      if (index < current) showStep(index, true);
    });
    progress.querySelector('.intake-stages').append(button);
    return button;
  });

  for (const field of form.querySelectorAll('[data-intake-field]')) {
    const feedback = document.createElement('span');
    feedback.className = 'field-feedback';
    feedback.id = field.name + '-feedback';
    feedback.setAttribute('aria-live', 'polite');
    field.after(feedback);
    field.setAttribute('aria-describedby', [field.getAttribute('aria-describedby'), feedback.id].filter(Boolean).join(' '));

    function validate() {
      field.setCustomValidity('');
      const error = (rules[field.name] && rules[field.name](field.value)) || field.validationMessage;
      field.setCustomValidity(error || '');
      field.setAttribute('aria-invalid', String(Boolean(error)));
      feedback.textContent = error || (field.name === 'receipt' || field.name === 'a_numbers' ? 'Format looks right' : '');
      return !error;
    }
    validators.set(field, validate);
    field.addEventListener('blur', validate);
    field.addEventListener('change', validate);
    field.addEventListener('input', () => {
      field.setCustomValidity('');
      field.removeAttribute('aria-invalid');
      feedback.textContent = '';
    });
  }

  function showStep(index, focus = false) {
    current = index;
    steps.forEach((step, i) => { step.hidden = i !== index; });
    position.textContent = `Step ${index + 1} of ${steps.length}`;
    meter.value = index + 1;
    stages.forEach((button, i) => {
      button.disabled = i >= index;
      button.dataset.completed = String(i < index);
      if (i === index) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    next.textContent = index + 1 < steps.length
      ? 'Continue to ' + steps[index + 1].dataset.stepLabel.toLowerCase()
      : 'Continue';
    back.hidden = index === 0;
    if (cancel) cancel.hidden = index !== 0;
    next.hidden = index === steps.length - 1;
    submit.hidden = !next.hidden;
    if (focus) steps[index].querySelector('legend').focus();
  }

  function firstInvalid(step) {
    return Array.from(step.querySelectorAll('input')).find(field => {
      const validate = validators.get(field);
      return validate ? !validate() : !field.validity.valid;
    });
  }

  function advance() {
    const invalid = firstInvalid(steps[current]);
    if (invalid) invalid.focus();
    else showStep(current + 1, true);
  }

  form.noValidate = true;
  form.classList.add('intake-guided');
  progress.hidden = false;
  meter.max = steps.length;
  showStep(0);
  next.addEventListener('click', advance);
  back.addEventListener('click', () => showStep(current - 1, true));
  form.addEventListener('submit', event => {
    if (current < steps.length - 1) {
      event.preventDefault();
      advance();
      return;
    }
    for (const [index, step] of steps.entries()) {
      const invalid = firstInvalid(step);
      if (invalid) {
        event.preventDefault();
        showStep(index);
        invalid.focus();
        return;
      }
    }
  });
})();
