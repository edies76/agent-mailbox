'use strict';

fetch('/health', { cache: 'no-store' })
  .then(response => response.ok ? response.json() : Promise.reject())
  .then(health => {
    document.querySelectorAll('[data-version]').forEach(element => {
      element.textContent = `v${health.version}`;
    });
  })
  .catch(() => {
    document.querySelectorAll('[data-version]').forEach(element => {
      element.textContent = 'Agent Mailbox';
    });
  });
