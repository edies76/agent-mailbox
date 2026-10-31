(() => {
  const list = document.getElementById('profiles-list');
  const error = document.getElementById('profiles-error');
  const make = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };

  function renderInstance(instance) {
    const card = make('article', 'unprofiled-instance');
    const top = make('div', 'unprofiled-instance-top');
    top.append(make('strong', '', instance.instance_name || instance.name), make('span', 'presence-dot' + (instance.online ? ' online' : ''), instance.online ? 'En línea' : 'Inactiva'));
    card.append(top);
    card.append(make('p', '', (instance.kind || 'agente') + ' · ' + (instance.project || 'sin proyecto')));
    const provenance = instance.added_by || {};
    card.append(make('small', '', 'Añadido por ' + (provenance.username || 'sistema') + ' · ' + (provenance.method || 'registro directo') + ' · ID ' + instance.id));
    return card;
  }

  function render(data) {
    list.replaceChildren();
    const group = new URLSearchParams(location.search).get('group');
    const unprofiled = data.profiles.find(profile => profile.unprofiled);
    if (group === 'unprofiled' && unprofiled) {
      const back = make('a', 'back-link inline-back', '← Todos los perfiles');
      back.href = '/dashboard/profiles';
      list.append(back, make('div', 'unprofiled-heading', 'Instancias sin perfil · ' + unprofiled.instances_count));
      const grid = make('div', 'unprofiled-grid');
      for (const instance of unprofiled.instances || []) grid.append(renderInstance(instance));
      list.append(grid);
      return;
    }
    if (!data.profiles.length) {
      list.append(make('div', 'empty', 'Todavía no hay agentes en este workspace.'));
      return;
    }
    for (const profile of data.profiles) {
      const card = make('a', 'profile-index-card' + (profile.unprofiled ? ' unprofiled-card' : ''));
      card.href = profile.route;
      const meta = make('div', 'profile-index-meta');
      meta.append(make('span', 'eyebrow', profile.unprofiled ? 'SIN PERFIL' : profile.kind),
                  make('span', 'pill', profile.instances_count + ' instancia' + (profile.instances_count === 1 ? '' : 's')));
      card.append(meta, make('h2', '', profile.name),
        make('p', '', profile.description || profile.project || 'Sin descripción pública.'),
        make('small', '', profile.unprofiled
          ? profile.instances_count + ' agentes pendientes de agrupar · abrir grupo →'
          : profile.online_count + ' en línea · abrir perfil →'));
      list.append(card);
    }
  }

  async function load() {
    try {
      const session = await fetch('/auth/session', {credentials: 'same-origin'});
      if (!session.ok) { location.href = '/dashboard'; return; }
      const response = await fetch('/dashboard/profiles/data', {credentials: 'same-origin', cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
      render(data);
    } catch (cause) {
      error.textContent = cause.message;
      list.replaceChildren(make('div', 'empty', 'No se pudieron cargar los agentes. Vuelve al dashboard e inténtalo de nuevo.'));
    }
  }
  load();
})();
