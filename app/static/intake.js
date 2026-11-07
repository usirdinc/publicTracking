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
  for (const field of form.querySelectorAll('[data-intake-field]')) {
    const feedback = document.createElement('span');
    feedback.className = 'field-feedback';
    feedback.id = field.name + '-feedback';
    feedback.setAttribute('aria-live', 'polite');
    (field === date ? field.parentElement : field).after(feedback);
    field.setAttribute('aria-describedby', [field.getAttribute('aria-describedby'), feedback.id].filter(Boolean).join(' '));

    function validate() {
      field.setCustomValidity('');
      const error = (rules[field.name] && rules[field.name](field.value)) || field.validationMessage;
      field.setCustomValidity(error || '');
      field.setAttribute('aria-invalid', String(Boolean(error)));
      feedback.textContent = error || (field.name === 'receipt' || field.name === 'a_numbers' ? 'Format looks right' : '');
    }
    field.addEventListener('blur', validate);
    field.addEventListener('change', validate);
    field.addEventListener('invalid', validate);
    field.addEventListener('input', () => {
      field.setCustomValidity('');
      field.removeAttribute('aria-invalid');
      feedback.textContent = '';
    });
    form.addEventListener('submit', event => {
      validate();
      if (!field.validity.valid) event.preventDefault();
    });
  }
  form.addEventListener('submit', event => {
    if (event.defaultPrevented) form.reportValidity();
  });

  const opener = document.querySelector('[data-open-birthday]');
  const dial = document.createElement('dialog');
  if (typeof dial.showModal !== 'function') return;
  dial.className = 'birthday-dial';
  dial.setAttribute('aria-labelledby', 'birthday-heading');
  dial.innerHTML = `<h2 id="birthday-heading">Date of birth</h2>
    <p class="hint">Swipe up or down on a value, use the arrows, or type it in.</p>
    <div class="dial-columns"></div>
    <div class="dial-actions">
      <button type="button" class="btn ghost" data-dial-cancel>Cancel</button>
      <button type="button" class="btn" data-dial-apply>Use date</button>
    </div>`;
  document.body.append(dial);
  const columns = dial.querySelector('.dial-columns');
  const apply = dial.querySelector('[data-dial-apply]');
  const parts = ['Month', 'Day', 'Year'].map((name, index) => {
    const column = document.createElement('div');
    column.className = 'dial-column';
    column.innerHTML = `<label for="dial-${index}">${name}</label>
      <button type="button" aria-label="Increase ${name.toLowerCase()}">▲</button>
      <input id="dial-${index}" type="number" inputmode="numeric" step="1" required>
      <button type="button" aria-label="Decrease ${name.toLowerCase()}">▼</button>`;
    columns.append(column);
    const input = column.querySelector('input');
    input.min = '1';
    input.max = [12, 31, year][index];
    const step = delta => {
      input.value = Math.max(Number(input.min), Math.min(Number(input.max), (input.valueAsNumber || 1) + delta));
      sync();
    };
    const buttons = column.querySelectorAll('button');
    buttons[0].addEventListener('click', () => step(1));
    buttons[1].addEventListener('click', () => step(-1));
    input.addEventListener('input', () => sync());
    let start;
    input.addEventListener('touchstart', event => { start = event.touches[0].clientY; }, { passive: true });
    input.addEventListener('touchend', event => {
      const distance = start - event.changedTouches[0].clientY;
      if (Math.abs(distance) < 18) return;
      event.preventDefault();
      step(Math.trunc(distance / 18));
    }, { passive: false });
    return input;
  });

  function sync() {
    const [month, day, birthYear] = parts.map(input => input.valueAsNumber);
    const leap = birthYear % 4 === 0 && (birthYear % 100 !== 0 || birthYear % 400 === 0);
    parts[1].max = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] || 31;
    apply.disabled = !parts.every(input => input.validity.valid) || isoDate(birthYear, month, day) > date.max;
  }

  opener.hidden = false;
  opener.addEventListener('click', () => {
    const [y, m, d] = (date.value || (year - 30) + '-01-01').split('-').map(Number);
    [m, d, y].forEach((value, index) => { parts[index].value = value; });
    sync();
    dial.showModal();
  });
  dial.querySelector('[data-dial-cancel]').addEventListener('click', () => dial.close());
  apply.addEventListener('click', () => {
    const [month, day, birthYear] = parts.map(input => input.valueAsNumber);
    date.value = isoDate(birthYear, month, day);
    date.dispatchEvent(new Event('change', { bubbles: true }));
    dial.close();
  });
})();
