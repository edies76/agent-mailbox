# AWS independiente

Esta plantilla crea un buzón AWS separado. No comparte Firestore ni el historial de Cloud Run.

La cuenta verificada para este entorno es `457106204837` y la región usada es `us-east-2`. La plantilla
usa una instancia `t4g.small`, un EBS de datos cifrado, una IP pública fija, SSM Session Manager y un
bucket S3 privado para respaldos. El acceso web se hace por `mailbox.bambalunar.app`; el DNS debe
apuntar a la salida `PublicIPv4` del stack.

## Crear la base de infraestructura

```powershell
aws cloudformation create-stack --profile "bambalogin@gmail.com" --region us-east-2 `
  --stack-name agent-mailbox-aws-prod --template-body file://stack.json `
  --capabilities CAPABILITY_IAM `
  --parameters ParameterKey=VpcId,ParameterValue=VPC_ID `
               ParameterKey=SubnetId,ParameterValue=SUBNET_ID `
               ParameterKey=AvailabilityZone,ParameterValue=us-east-2a `
               ParameterKey=DomainName,ParameterValue=mailbox.bambalunar.app
```

La plantilla no abre SSH. Se administra por SSM. El disco `/var/lib/agent-mailbox` se conserva al
eliminar el stack. El script `prepare-volume.sh` comprueba el serial de EBS antes de formatear y solo
formatea cuando recibe `MAILBOX_ALLOW_FORMAT_EMPTY=1`.

## Instalar una versión

La instancia debe ejecutar el commit publicado de `agent-mailbox-public` con `MAILBOX_STORAGE=sqlite`,
`MAILBOX_DB=/var/lib/agent-mailbox/data/mailbox.sqlite3`, `MAILBOX_LOCAL_FIRST_RUN=0` y dos secretos
generados dentro de Parameter Store. No copies claves AWS, Google Cloud ni secretos de aplicación al
repositorio, a user-data, a una imagen Docker ni a la salida de una orden SSM.

El proxy HTTPS debe emitir el certificado para `mailbox.bambalunar.app` y pasar el puerto interno
`8795`. Caddy es el proxy previsto; su estado se conserva en `/var/lib/agent-mailbox/caddy-data` y
`/var/lib/agent-mailbox/caddy-config`.

Antes de dar la URL a agentes, comprueba:

```powershell
Invoke-RestMethod https://mailbox.bambalunar.app/health
Invoke-RestMethod https://mailbox.bambalunar.app/capabilities
```

El buzón AWS tendrá su propia `MAILBOX_ADMIN_KEY` y `MAILBOX_JOIN_KEY`. La cuenta humana y los agentes
se crean desde cero. Cloud Run continúa usando su URL y sus secretos actuales.

## Costos

La cuenta nueva está en el plan FREE con créditos. `t4g.small` tiene una promoción temporal de 750
horas mensuales hasta el 31 de diciembre de 2026; EBS, IP, tráfico y excesos de CPU se revisan aparte.
Después de la promoción, detén o elimina la instancia si no quieres cargos. La plantilla no crea NAT
Gateway, Load Balancer ni una IP adicional.
