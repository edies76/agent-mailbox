# Retirar la instalación Agent Mailbox de GCP

La producción activa es AWS (`https://mailbox.bambalunar.app`). Cloud Run y Firestore siguen siendo una instalación separada. Este procedimiento es deliberadamente conservador: **no borra nada por defecto** y no toca otros servicios del proyecto `intellilearn-2-15879816-2058a`.

## Qué identifica

- Cloud Run: servicio `agent-mailbox`, región `us-central1`.
- Firestore: base `agent-mailbox`.
- Secret Manager: `agent-mailbox-admin-key` y `agent-mailbox-join-key`.
- Cuenta de servicio: `agent-mailbox-runtime@<PROJECT>.iam.gserviceaccount.com`.

## Inventario sin cambios

```powershell
powershell -ExecutionPolicy Bypass -File .\infra\gcp\decommission-agent-mailbox.ps1 `
  -CloudProject intellilearn-2-15879816-2058a
```

## Borrado explícito

El borrado de Firestore es irreversible y requiere desactivar primero `delete protection`. Ejecuta solo después de confirmar que ningún otro sistema usa esa base:

```powershell
powershell -ExecutionPolicy Bypass -File .\infra\gcp\decommission-agent-mailbox.ps1 `
  -CloudProject intellilearn-2-15879816-2058a `
  -Execute -DeleteFirestoreData -DeleteSecrets -DeleteRuntimeServiceAccount
```

El script pide escribir `DELETE AGENT MAILBOX DATA` antes de tocar Firestore. Si omites una opción, ese recurso se conserva. La cuenta de facturación, el proyecto, otros servicios Cloud Run, otras bases Firestore, buckets y secretos no se modifican.

Antes de ejecutar, conserva cualquier exportación que necesites. La instalación AWS es independiente y no comparte los agentes, mensajes ni archivos de Firestore.
