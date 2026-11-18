#!/usr/bin/env bash
# Install junction-lab on a Debian/Ubuntu/Armbian box (center and/or controllers).
#   sudo ./deploy/install.sh            # installs to /opt/junction-lab
set -euo pipefail
PREFIX=/opt/junction-lab
id jl >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin jl
mkdir -p "$PREFIX" /etc/junction-lab /var/lib/junction-lab
cp -r pyproject.toml uv.lock README.md src configs "$PREFIX"/
[ -d web/dist ] && cp -r web/dist "$PREFIX"/web/ || echo "web/dist missing: run 'npm --prefix web run build' for the panel"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
cd "$PREFIX" && uv venv && uv pip install .
chown -R jl:jl "$PREFIX" /var/lib/junction-lab
install -m 644 deploy/systemd/*.service /etc/systemd/system/ 2>/dev/null || install -m 644 "$OLDPWD"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
echo "done. configure /etc/junction-lab/*.env then: systemctl enable --now jl-center jl-controller@1"
echo "logs: journalctl -u 'jl-*' -f"
