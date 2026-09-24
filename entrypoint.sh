#!/bin/sh
set -e

# Default PUID/PGID (Unraid typically uses 99:100, Linux desktops 1000:1000)
PUID=${PUID:-1000}
PGID=${PGID:-1000}
UMASK=${UMASK:-022}

umask "$UMASK"

# If running as root inside container, configure user/group and drop privileges
if [ "$(id -u)" = "0" ]; then
    # Ensure group with PGID exists
    if ! getent group "$PGID" >/dev/null 2>&1; then
        groupadd -o -g "$PGID" appgroup 2>/dev/null || true
    fi

    # Ensure user with PUID exists and is assigned to PGID
    if ! getent passwd "$PUID" >/dev/null 2>&1; then
        useradd -o -u "$PUID" -g "$PGID" -m -s /bin/sh appuser 2>/dev/null || true
    else
        existing_user=$(getent passwd "$PUID" | cut -d: -f1)
        usermod -o -g "$PGID" "$existing_user" 2>/dev/null || true
    fi

    # Fix ownership of data directory if present
    if [ -d "/app/data" ]; then
        chown -R "$PUID:$PGID" /app/data 2>/dev/null || true
    fi

    # Drop root privileges and execute command
    if command -v gosu >/dev/null 2>&1; then
        exec gosu "$PUID:$PGID" "$@"
    elif command -v su-exec >/dev/null 2>&1; then
        exec su-exec "$PUID:$PGID" "$@"
    else
        exec "$@"
    fi
else
    # Container started with specific non-root user (e.g. docker run --user ...)
    exec "$@"
fi
