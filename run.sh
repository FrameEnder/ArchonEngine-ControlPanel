#!/usr/bin/env bash
# Build and run Kawari Web in rootless Podman.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IMAGE:-kawari-web}"
NAME="${NAME:-kawari-web}"
PORT="${PORT:-7301}"
DATA_DIR="${DATA_DIR:-$HOME/.kawari}"
SOCKET="${SOCKET:-/run/user/$(id -u)/podman/podman.sock}"

if [[ ! -d "$DATA_DIR" ]]; then
    echo "ERROR: $DATA_DIR does not exist. Set DATA_DIR to your Kawari config directory." >&2
    exit 1
fi

if [[ ! -w "$DATA_DIR" ]]; then
    echo "ERROR: $DATA_DIR is not writable by $(id -un)." >&2
    exit 1
fi

# Remove any container from a previous run AND any quadlet-managed one, so an
# old image can never keep restarting behind a fresh build.
if systemctl --user is-active --quiet kawari-web 2>/dev/null; then
    echo "Stopping the systemd (quadlet) unit first..."
    systemctl --user stop kawari-web || true
fi

echo "Building $IMAGE..."
podman build -t "$IMAGE" --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
    -f "$here/Containerfile" "$here"

socket_args=()
if [[ -S "$SOCKET" ]]; then
    echo "Podman API socket found; the dashboard will be able to control the server."
    # SELinux labels the socket for your user session, and a confined container
    # process cannot write to it - that shows up as PermissionError [Errno 13]
    # even though the uid is correct. Relabelling the socket would affect the
    # host, so disable labelling for THIS container instead.
    socket_args=(-v "$SOCKET:/run/podman.sock" -e PODMAN_SOCKET=/run/podman.sock)
    if command -v selinuxenabled >/dev/null && selinuxenabled 2>/dev/null; then
        echo "SELinux is enforcing; adding --security-opt label=disable so the socket"
        echo "  is reachable (the data mount keeps its :z label)."
        socket_args+=(--security-opt label=disable)
    fi
else
    echo "NOTE: no Podman socket at $SOCKET - start/stop/logs will be disabled."
    echo "      Enable it with: systemctl --user enable --now podman.socket"
fi

# SELinux: the data directory is shared with the Kawari server container, so use
# :z (shared) rather than :Z (private), which would break the other container.
podman rm -f "$NAME" >/dev/null 2>&1 || true
# --userns=keep-id maps your host UID to the SAME UID inside the container.
# Without it, rootless Podman maps container UID 1000 to a subuid (100999-ish)
# and your ~/.kawari appears root-owned inside, so the app cannot write to it.
podman run -d --name "$NAME" --restart unless-stopped \
    --userns=keep-id \
    --add-host=host.containers.internal:host-gateway \
    -p "${PORT}:7301" \
    -v "$DATA_DIR:/data:z" \
    -e KAWARI_HOST_DATA_DIR="$DATA_DIR" \
    "${socket_args[@]}" \
    "$IMAGE"

echo
sleep 2
echo
echo "Image:     $(podman image inspect "$IMAGE" --format '{{.Id}}' | cut -c1-19) built $(podman image inspect "$IMAGE" --format '{{.Created}}' | cut -c1-19)"
echo "Container: $(podman inspect "$NAME" --format '{{.Id}}' | cut -c1-12) created $(podman inspect "$NAME" --format '{{.Created}}' | cut -c1-19)"
echo "--- first log lines (should say the version and uid) ---"
podman logs "$NAME" 2>&1 | head -12
echo "--------------------------------------------------------"
# Deliberately 127.0.0.1 and not "localhost": localhost usually resolves to ::1
# first, and rootless Podman publishes on IPv4 only, so the v6 attempt is refused
# with no fallback.
echo "Kawari Web is starting on http://127.0.0.1:${PORT}"
LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
for addr in $LAN_IP $TS_IP; do
    echo "  from other machines: http://${addr}:${PORT}"
done
echo "  (use 127.0.0.1 rather than localhost - localhost resolves to ::1 first)"
echo "Host data dir passed through as KAWARI_HOST_DATA_DIR=$DATA_DIR"
echo "First visit will ask you to create or adopt an administrator account."
