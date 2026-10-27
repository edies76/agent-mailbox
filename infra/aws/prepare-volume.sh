#!/usr/bin/env bash
set -euo pipefail

: "${MAILBOX_DATA_VOLUME_ID:?Set the exact EBS volume ID}"
[[ "$MAILBOX_DATA_VOLUME_ID" =~ ^vol-[0-9a-f]+$ ]] || { echo 'Invalid EBS volume ID' >&2; exit 1; }
serial="${MAILBOX_DATA_VOLUME_ID//-/}"
device="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${serial}"
for _ in $(seq 1 60); do [[ -b "$device" ]] && break; sleep 2; done
[[ -b "$device" ]] || { echo 'Expected EBS device is unavailable' >&2; exit 1; }
actual="$(lsblk -dn -o SERIAL "$(readlink -f "$device")" | tr -d '[:space:]')"
[[ "$actual" == "$serial" ]] || { echo 'EBS serial does not match the requested volume' >&2; exit 1; }
root_source="$(findmnt -n -o SOURCE /)"
root_parent="$(lsblk -no PKNAME "$root_source" | head -n 1)"
[[ "$(basename "$(readlink -f "$device")")" != "$root_parent" ]] || { echo 'Refusing to touch the root disk' >&2; exit 1; }

mountpoint=/var/lib/agent-mailbox
install -d -m 0700 "$mountpoint"
if mountpoint -q "$mountpoint"; then
  [[ "$(readlink -f "$(findmnt -n -o SOURCE "$mountpoint")")" == "$(readlink -f "$device")" ]] || { echo 'Another volume is already mounted there' >&2; exit 1; }
else
  filesystem="$(blkid -s TYPE -o value "$device" || true)"
  if [[ -z "$filesystem" ]]; then
    [[ "${MAILBOX_ALLOW_FORMAT_EMPTY:-0}" == 1 ]] || { echo 'Explicit permission is required to format an empty volume' >&2; exit 1; }
    if ! signatures="$(wipefs -n --noheadings --output TYPE "$device")"; then echo 'Could not inspect disk signatures; refusing to format' >&2; exit 1; fi
    [[ -z "$signatures" ]] || { echo 'Unexpected disk signature; refusing to format' >&2; exit 1; }
    mkfs.ext4 -F -L agent-mailbox "$device"
  elif [[ "$filesystem" != ext4 ]]; then
    echo 'Unexpected filesystem; refusing to change it' >&2
    exit 1
  fi
  uuid="$(blkid -s UUID -o value "$device")"
  existing="$(awk -v target="$mountpoint" '$2 == target {print $1}' /etc/fstab)"
  [[ -z "$existing" || "$existing" == "UUID=$uuid" ]] || { echo 'An unrelated fstab entry already uses this mountpoint' >&2; exit 1; }
  if [[ -z "$existing" ]]; then printf 'UUID=%s %s ext4 defaults,nofail,noatime 0 2\n' "$uuid" "$mountpoint" >> /etc/fstab; fi
  mount "$mountpoint"
fi
install -d -m 0700 "$mountpoint/data" "$mountpoint/private" "$mountpoint/backup-staging" "$mountpoint/caddy-data" "$mountpoint/caddy-config"
echo 'Agent Mailbox data volume is ready.'
