#!/bin/sh
# Startet als root, repariert die Rechte des Daten-Volumes und gibt die Rechte dann ab.
# Bind-Mounts wie /mnt/user/appdata/... legt Docker sonst als root an, sodass der Dienst nicht schreiben kann.
set -e

PUID="${PUID:-99}"   # Unraid: nobody
PGID="${PGID:-100}"  # Unraid: users

mkdir -p /data
if [ "$(stat -c '%u:%g' /data)" != "$PUID:$PGID" ]; then
    chown -R "$PUID:$PGID" /data
fi

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
