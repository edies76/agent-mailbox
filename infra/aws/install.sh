#!/usr/bin/env bash
set -euo pipefail

: "${AGENT_MAILBOX_COMMIT:?Set the immutable git commit to deploy}"
: "${AGENT_MAILBOX_BUCKET:?Set the private backup bucket name}"
: "${AGENT_MAILBOX_REGION:?Set the AWS region}"
: "${AGENT_MAILBOX_DOMAIN:?Set the public HTTPS hostname}"

root=/opt/agent-mailbox
data=/var/lib/agent-mailbox
repo="$root/repo"
private="$data/private"
mkdir -p "$root" "$private" "$data/data" "$data/backup-staging" "$data/caddy-data" "$data/caddy-config"
chmod 700 "$private" "$data/data" "$data/backup-staging"

if [[ ! -d "$repo/.git" ]]; then
  git clone https://github.com/edies76/agent-mailbox.git "$repo"
else
  git -C "$repo" fetch --quiet origin main
fi
git -C "$repo" checkout --quiet --detach "$AGENT_MAILBOX_COMMIT"

admin_key="$(aws ssm get-parameter --name "/agent-mailbox/agent-mailbox-aws-prod/admin-key" --with-decryption --query Parameter.Value --output text)"
join_key="$(aws ssm get-parameter --name "/agent-mailbox/agent-mailbox-aws-prod/join-key" --with-decryption --query Parameter.Value --output text)"
[[ ${#admin_key} -ge 32 && ${#join_key} -ge 16 ]] || { echo 'Application secrets are too short' >&2; exit 1; }
umask 077
printf 'MAILBOX_ADMIN_KEY=%s\nMAILBOX_JOIN_KEY=%s\nMAILBOX_STORAGE=sqlite\nMAILBOX_DB=/data/mailbox.sqlite3\nMAILBOX_LOCAL_FIRST_RUN=0\nMAILBOX_RELEASE=%s\nMAILBOX_PUBLIC_URL=https://%s\nMAILBOX_MCP_ALLOWED_HOSTS=%s,%s:*\nMAILBOX_ONLINE_SECONDS=90\n' \
  "$admin_key" "$join_key" "$(cat "$repo/VERSION")" "$AGENT_MAILBOX_DOMAIN" "$AGENT_MAILBOX_DOMAIN" "$AGENT_MAILBOX_DOMAIN" > "$private/app.env"
unset admin_key join_key

docker build --pull -t "agent-mailbox:$(cat "$repo/VERSION")" "$repo"
docker rm -f agent-mailbox agent-mailbox-caddy >/dev/null 2>&1 || true
docker run -d --name agent-mailbox --restart unless-stopped --read-only --tmpfs /tmp --publish 127.0.0.1:8795:8795 \
  --env-file "$private/app.env" --volume "$data/data:/data" --volume "$data/backup-staging:/backup-staging" "agent-mailbox:$(cat "$repo/VERSION")" >/dev/null

cat > "$data/caddy-config/Caddyfile" <<EOF
$AGENT_MAILBOX_DOMAIN {
  encode gzip
  reverse_proxy 127.0.0.1:8795
}
EOF
chmod 600 "$data/caddy-config/Caddyfile"
docker run -d --name agent-mailbox-caddy --restart unless-stopped --network host \
  --volume "$data/caddy-data:/data" --volume "$data/caddy-config:/config" \
  --volume "$data/caddy-config/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine \
  caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

cat > /usr/local/sbin/agent-mailbox-backup <<EOF
#!/usr/bin/env bash
set -euo pipefail
stamp=\$(date -u +%Y%m%dT%H%M%SZ)
target="$data/backup-staging/mailbox-\$stamp.sqlite3"
docker exec -e MAILBOX_BACKUP_NAME="mailbox-\$stamp.sqlite3" agent-mailbox python -c 'import os,sqlite3; src=sqlite3.connect("/data/mailbox.sqlite3"); dst=sqlite3.connect("/backup-staging/"+os.environ["MAILBOX_BACKUP_NAME"]); src.backup(dst); dst.close(); src.close()'
aws s3 cp "\$target" "s3://$AGENT_MAILBOX_BUCKET/daily/\$(basename "\$target")" --sse AES256 --region "$AGENT_MAILBOX_REGION" >/dev/null
rm -f "\$target"
EOF
chmod 700 /usr/local/sbin/agent-mailbox-backup
cat > /etc/systemd/system/agent-mailbox-backup.service <<'EOF'
[Unit]
Description=Agent Mailbox SQLite backup
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/agent-mailbox-backup
EOF
cat > /etc/systemd/system/agent-mailbox-backup.timer <<'EOF'
[Unit]
Description=Daily Agent Mailbox backup

[Timer]
OnCalendar=*-*-* 03:15:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now agent-mailbox-backup.timer
sleep 5
curl --fail --silent --show-error http://127.0.0.1:8795/health
echo
echo "Agent Mailbox $(cat "$repo/VERSION") installed; public host: $AGENT_MAILBOX_DOMAIN"
