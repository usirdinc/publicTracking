(() => {
  const t = window.USIRDLocale.t;
  const profile = document.querySelector('[data-profile-photo]');
  if (!profile) return;
  const file = profile.querySelector('input');
  const canvas = profile.querySelector('canvas');
  const image = profile.querySelector('img');
  const initial = profile.querySelector('.profile-initial');
  const save = profile.querySelector('[data-photo-save]');
  const remove = profile.querySelector('[data-photo-remove]');
  const status = profile.querySelector('[role="status"]');
  let version = 0;
  let saved = !image.hidden;

  function showPhoto(preview = false) {
    canvas.hidden = save.hidden = !preview;
    image.hidden = preview || !saved;
    initial.hidden = preview || saved;
    remove.hidden = !saved;
  }

  file.addEventListener('change', async () => {
    const selection = ++version;
    showPhoto();
    const selected = file.files[0];
    if (!selected) return;
    if (selected.size > 8 * 1024 * 1024) {
      status.textContent = t('Choose an image under 8 MB.');
      return;
    }
    try {
      const bitmap = await createImageBitmap(selected);
      try {
        if (selection !== version) return;
        const size = Math.min(bitmap.width, bitmap.height);
        const context = canvas.getContext('2d', { alpha: false });
        context.fillStyle = '#fff';
        context.fillRect(0, 0, 256, 256);
        context.drawImage(bitmap, (bitmap.width - size) / 2, (bitmap.height - size) / 2,
                          size, size, 0, 0, 256, 256);
        showPhoto(true);
        status.textContent = t('Preview ready. Select Save photo to apply it.');
      } finally {
        bitmap.close();
      }
    } catch {
      if (selection === version) status.textContent = t('Choose a readable JPEG, PNG, or WebP image.');
    }
  });

  async function update(method) {
    ++version;
    file.disabled = save.disabled = remove.disabled = true;
    try {
      const headers = { 'X-CSRF-Token': profile.dataset.csrf };
      const options = { method, headers };
      if (method === 'PUT') {
        options.body = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .85));
        if (!options.body) throw new Error('No image');
        headers['Content-Type'] = 'image/jpeg';
      }
      const response = await fetch('/account/photo', options);
      if (!response.ok) throw new Error('Photo request failed');
      saved = method === 'PUT';
      if (saved) image.src = '/account/photo?v=' + Date.now();
      else image.removeAttribute('src');
      file.value = '';
      showPhoto();
      status.textContent = saved ? t('Profile photo saved.') : t('Profile photo removed.');
    } catch {
      status.textContent = t('Could not update your profile photo. Please try again.');
    } finally {
      file.disabled = save.disabled = remove.disabled = false;
    }
  }
  save.addEventListener('click', () => update('PUT'));
  remove.addEventListener('click', () => update('DELETE'));
})();
