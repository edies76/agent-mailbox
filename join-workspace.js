(() => {
  const form = document.getElementById('workspace-signup-form');
  const code = document.getElementById('invite-code');
  const status = document.getElementById('signup-status');
  const params = new URLSearchParams(location.hash.slice(1));
  code.value = params.get('invite') || '';
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    status.className = 'status';
    status.textContent = 'Creando tu cuenta privada…';
    try {
      const response = await fetch('/auth/register', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        cache: 'no-store',
        body: JSON.stringify({
          invite_code: code.value.trim(),
          workspace_name: document.getElementById('workspace-name').value.trim(),
          username: document.getElementById('signup-username').value.trim(),
          password: document.getElementById('signup-password').value
        })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `HTTP ${response.status}`);
      document.getElementById('signup-password').value = '';
      status.className = 'status success';
      status.textContent = 'Tu espacio quedó conectado. Abriendo tu panel…';
      location.assign('/dashboard');
    } catch (error) {
      status.className = 'status error';
      status.textContent = error.message;
      button.disabled = false;
    }
  });
})();
