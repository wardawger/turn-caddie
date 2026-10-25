#!/usr/bin/env bash
# One-shot installer for the AutoHedge runner on Ubuntu/Debian. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/wardawger/turn-caddie/claude/amazing-mendel-mcxtn5/autohedge-ops/setup.sh | sudo bash
# Re-run it to upgrade. It never touches an existing bot/.env.
set -euo pipefail
BRANCH=claude/amazing-mendel-mcxtn5
APP=/opt/autohedge-ops

apt-get update -qq && apt-get install -y -qq git python3-venv >/dev/null
id autohedge >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin autohedge

if [ -d "$APP/repo/.git" ]; then
  git -C "$APP/repo" fetch -q origin "$BRANCH" && git -C "$APP/repo" reset -q --hard "origin/$BRANCH"
else
  mkdir -p "$APP" && git clone -q -b "$BRANCH" https://github.com/wardawger/turn-caddie "$APP/repo"
fi

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q -r "$APP/repo/autohedge-ops/requirements.txt"

BOT="$APP/repo/autohedge-ops/bot"
if [ ! -f "$BOT/.env" ]; then
  cp "$BOT/.env.example" "$BOT/.env"
  echo "Created $BOT/.env: fill in SUPABASE_SERVICE_KEY, OPENAI_API_KEY, EXA_API_KEY."
fi
chown -R autohedge:autohedge "$APP"; chmod 600 "$BOT/.env"

sed -e "s#/opt/autohedge-ops/bot#$BOT#" -e "s#/opt/autohedge-ops/.venv#$APP/.venv#" \
  "$BOT/autohedge-bot.service" > /etc/systemd/system/autohedge-bot.service
systemctl daemon-reload && systemctl enable autohedge-bot >/dev/null
echo "Next: nano $BOT/.env  then  systemctl restart autohedge-bot && journalctl -fu autohedge-bot"
