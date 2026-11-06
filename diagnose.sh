#!/usr/bin/env bash
# ArchonEngine ControlPanel diagnostics - answers "which build is running, and who can write what".
NAME="${NAME:-kawari-web}"
IMAGE="${IMAGE:-kawari-web}"
DATA_DIR="${DATA_DIR:-$HOME/.kawari}"

echo "=== host ==="
echo "user: $(id -un) uid=$(id -u) gid=$(id -g)"
echo "data dir: $DATA_DIR"
ls -ld "$DATA_DIR" 2>/dev/null || echo "  MISSING"
ls -ld "$DATA_DIR/web" 2>/dev/null || echo "  $DATA_DIR/web does not exist yet (normal before first successful start)"
echo "writable by you: $([[ -w "$DATA_DIR" ]] && echo yes || echo NO)"
command -v getenforce >/dev/null && echo "selinux: $(getenforce)"

echo
echo "=== settings.conf paths vs reality ==="
CONF="$DATA_DIR/settings.conf"
if [[ -f "$CONF" ]]; then
    for key in DATA_DIR GAME_PATH; do
        val=$(grep "^$key=" "$CONF" | cut -d= -f2- | tr -d "\"'")
        if [[ -z "$val" ]]; then
            echo "  $key: (not set)"
        elif [[ -e "$val" ]]; then
            echo "  $key: $val  [exists]"
        else
            echo "  $key: $val  [MISSING on this machine]"
        fi
    done
    echo "  databases actually present:"
    for f in "$DATA_DIR/data/world.db" "$DATA_DIR/data/login.db"; do
        [[ -f "$f" ]] && echo "    $f ($(du -h "$f" | cut -f1))" || echo "    $f absent"
    done
else
    echo "  no settings.conf at $CONF"
fi

echo
echo "=== subuid mapping ==="
grep "^$(id -un):" /etc/subuid 2>/dev/null || echo "  no /etc/subuid entry for $(id -un)"

echo
echo "=== containers named or imaged like kawari-web ==="
podman ps -a --filter "name=$NAME" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.CreatedAt}}' 2>/dev/null
echo
echo "all containers using image $IMAGE:"
podman ps -a --filter "ancestor=$IMAGE" --format '  {{.Names}} ({{.Status}})' 2>/dev/null

echo
echo "=== systemd (quadlet) unit ==="
systemctl --user is-active kawari-web 2>/dev/null || echo "  not managed by systemd (or inactive)"

echo
echo "=== image ==="
podman image inspect "$IMAGE" --format '  id: {{.Id}}{{"\n"}}  created: {{.Created}}' 2>/dev/null \
  || echo "  image $IMAGE not built"

echo
echo "=== what the running container actually sees ==="
if podman inspect "$NAME" >/dev/null 2>&1; then
  echo "  userns setting: $(podman inspect "$NAME" --format '{{.HostConfig.UsernsMode}}')"
  podman exec "$NAME" sh -c 'id; ls -ld /data /data/web 2>&1' 2>/dev/null \
    || echo "  (container not running - cannot exec)"
  echo "  code version inside the image:"
  podman run --rm --entrypoint sh "$IMAGE" -c \
    'grep -c "Cannot write to" /app/kawari_web/config.py' 2>/dev/null \
    | sed 's/^/    matches of the 2.1.2 error handler: /'
else
  echo "  no container called $NAME"
fi

echo
echo "=== podman socket ==="
SOCKET="${SOCKET:-/run/user/$(id -u)/podman/podman.sock}"
if [[ -S "$SOCKET" ]]; then
    ls -lZ "$SOCKET" 2>/dev/null || ls -l "$SOCKET"
    echo "  host can talk to it: $(curl -s --unix-socket "$SOCKET" \
        http://d/v5.0.0/libpod/info >/dev/null 2>&1 && echo yes || echo NO)"
else
    echo "  no socket at $SOCKET"
    echo "    systemctl --user enable --now podman.socket"
fi
if podman inspect "$NAME" >/dev/null 2>&1; then
    echo "  container security opts: $(podman inspect "$NAME" --format '{{.HostConfig.SecurityOpt}}')"
    echo "  socket as the container sees it:"
    podman exec "$NAME" sh -c 'ls -l /run/podman.sock 2>&1; id -u' 2>/dev/null | sed 's/^/    /'
    echo "  container can talk to it:"
    podman exec "$NAME" python -c "
import http.client, socket
class C(http.client.HTTPConnection):
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect('/run/podman.sock'); self.sock = s
try:
    c = C('localhost'); c.request('GET', '/v5.0.0/libpod/info'); print('    OK', c.getresponse().status)
except Exception as exc:
    print('    FAILED:', exc)
" 2>/dev/null || echo "    (cannot exec)"
fi

echo
echo "=== is it actually listening? ==="
echo "podman port mapping:"
podman port "$NAME" 2>/dev/null | sed 's/^/  /' || echo "  none (container not running?)"

PORT="${PORT:-7301}"
echo
echo "host sockets on $PORT:"
if command -v ss >/dev/null || command -v netstat >/dev/null; then
    found=$( (ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep ":$PORT " )
    if [[ -n "$found" ]]; then
        echo "$found" | sed 's/^/  /'
    else
        echo "  NOTHING listening on $PORT - the container is not publishing it"
    fi
else
    echo "  (neither ss nor netstat installed - skipping; the HTTP probes below"
    echo "   are the reliable check anyway)"
fi

echo
echo "from inside the container:"
podman exec "$NAME" python -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:$PORT/connect', timeout=4)
    print('  app answers internally:', r.status)
except Exception as exc:
    print('  app does NOT answer inside the container:', exc)
" 2>/dev/null || echo "  (cannot exec - container not running)"

echo
echo "from this host:"
python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:$PORT/connect', timeout=4)
    print('  http://127.0.0.1:$PORT ->', r.status)
except Exception as exc:
    print('  http://127.0.0.1:$PORT FAILED:', exc)
" 2>/dev/null

LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
for addr in $LAN_IP $TS_IP; do
  python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://$addr:$PORT/connect', timeout=4)
    print('  http://$addr:$PORT ->', r.status)
except Exception as exc:
    print('  http://$addr:$PORT FAILED:', exc)
" 2>/dev/null
done
echo
echo "  Browse to one of the addresses above that answered."
echo "  Use the literal IP, not 'localhost': localhost resolves to ::1 (IPv6) first,"
echo "  and rootless Podman publishes IPv4 only, so localhost is refused."

echo
echo "=== firewall ==="
if command -v firewall-cmd >/dev/null; then
  echo "  default zone: $(firewall-cmd --get-default-zone 2>/dev/null)"
  echo "  active zones:"; firewall-cmd --get-active-zones 2>/dev/null | sed 's/^/    /'
  if firewall-cmd --query-port="$PORT/tcp" >/dev/null 2>&1; then
    echo "  port $PORT/tcp: ALLOWED"
  else
    echo "  port $PORT/tcp: NOT allowed - other machines cannot reach it"
    echo "    sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
  fi
else
  echo "  firewalld not present"
fi

echo
echo "=== last 15 log lines ==="
podman logs --tail 15 "$NAME" 2>&1 | sed 's/^/  /'
