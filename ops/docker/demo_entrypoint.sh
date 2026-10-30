#!/bin/sh
# Runs the synthetic demo API (flawed on 8800, fixed on 8801) and the platform demo runner.
# The runner token is created by the web container (bootstrap_demo_runner) in a shared volume.
set -eu
STATE=${DEMO_STATE_DIR:-/demo-state}
CONTROL_PLANE=${ACCESSGUARD_CONTROL_PLANE_URL:-http://web:8000}
python -m accessguard_sampleapp serve --variant flawed --port 8800 --credentials-out "$STATE/sampleapp-credentials.json" &
sleep 1
python -m accessguard_sampleapp serve --variant fixed --port 8801 --credentials-out "$STATE/sampleapp-credentials.json" &
while [ ! -s "$STATE/demo-runner.token" ]; do echo "waiting for the demo runner token"; sleep 2; done
cat > "$STATE/demo-runner.toml" <<CONFIG
poll_seconds = 3
[control_plane]
url = "$CONTROL_PLANE"
token_env = "ACCESSGUARD_RUNNER_TOKEN"
# The demo runner talks to the web container over the private compose network only.
allow_plain_http = true
[[targets]]
origin = "http://127.0.0.1:8800"
zone = "loopback"
credentials_file = "sampleapp-credentials.json"
[[targets]]
origin = "http://127.0.0.1:8801"
zone = "loopback"
credentials_file = "sampleapp-credentials.json"
CONFIG
ACCESSGUARD_RUNNER_TOKEN=$(cat "$STATE/demo-runner.token") exec python -m accessguard runner --config "$STATE/demo-runner.toml"
