'use strict';
for (const pre of document.querySelectorAll('pre')) {
  const button = document.createElement('button'); button.className = 'code-copy'; button.textContent = 'Copiar';
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(pre.querySelector('code').textContent); button.textContent = 'Copiado'; }
    catch { button.textContent = 'Selecciona el código'; }
  }); pre.append(button);
}
document.getElementById('copy-document').addEventListener('click', async () => {
  const status = document.getElementById('copy-status');
  try { await navigator.clipboard.writeText(document.getElementById('document-source').value); status.textContent = 'Documento copiado.'; }
  catch { status.textContent = 'No se pudo copiar. Selecciona el texto del documento.'; }
});
