#!/usr/bin/env bash
# Set up Tingposten on the Pi (Raspberry Pi OS Trixie, 64-bit).
# Safe to run again: every step checks before it changes anything.
#
#   git clone https://github.com/midttun/tingposten.git ~/tingposten
#   ~/tingposten/deploy/setup-pi.sh
set -euo pipefail

GITHUB_REPO="midttun/tingposten"  # owner/name; the account is not final yet
SSH_ALIAS="github-tingposten"
SSH_KEY="$HOME/.ssh/tingposten_deploy"

# Use the clone this script sits in; otherwise TINGPOSTEN_DIR or ~/tingposten.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${TINGPOSTEN_DIR:-}" ] && [ -d "$here/.git" ] && [ -f "$here/pyproject.toml" ]; then
  INSTALL_DIR="$here"
else
  INSTALL_DIR="${TINGPOSTEN_DIR:-$HOME/tingposten}"
fi

step() { printf '\n==> %s\n' "$*"; }

for cmd in git curl flock ssh-keygen; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
done

step "uv"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

step "repo in $INSTALL_DIR"
if [ ! -d "$INSTALL_DIR/.git" ]; then
  # The repo is public, so cloning needs no key. Pushing goes over SSH below.
  git clone "https://github.com/$GITHUB_REPO.git" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
git remote set-url --push origin "git@$SSH_ALIAS:$GITHUB_REPO.git"
git remote -v

step "python environment"
uv sync --frozen --no-dev

step ".env and data/"
if [ ! -f .env ]; then
  install -m 600 .env.example .env
  echo "created .env from .env.example"
fi
chmod 600 .env
mkdir -p data/logs
chmod +x deploy/*.sh

step "deploy key"
if [ ! -f "$SSH_KEY" ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  # No passphrase: cron has to push unattended. The key only opens this one repo.
  ssh-keygen -q -t ed25519 -N "" -C "tingposten-pi" -f "$SSH_KEY"
  echo "created $SSH_KEY"
fi

tz="$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"

cat <<EOF

Next steps
----------
1. Fill in the API keys and contact address:
     nano $INSTALL_DIR/.env

2. Add the deploy key on GitHub, with "Allow write access" ticked:
     https://github.com/$GITHUB_REPO/settings/keys/new
   Key:
     $(cat "$SSH_KEY.pub")

EOF

if grep -qs "^Host $SSH_ALIAS\$" "$HOME/.ssh/config"; then
  echo "3. ~/.ssh/config already has Host $SSH_ALIAS."
else
  cat <<EOF
3. Add this to ~/.ssh/config so pushes use the deploy key:

     Host $SSH_ALIAS
         HostName github.com
         User git
         IdentityFile $SSH_KEY
         IdentitiesOnly yes
EOF
fi

cat <<EOF

   Test it (should greet $GITHUB_REPO):
     ssh -T git@$SSH_ALIAS

4. Name the commits the Pi makes to gh-pages:
     git -C $INSTALL_DIR config user.name "Tingposten"
     git -C $INSTALL_DIR config user.email "<address>"

5. Time zone is: $tz$( [ "$tz" = Europe/Oslo ] || printf '  <- set it: sudo timedatectl set-timezone Europe/Oslo' )

6. Run once by hand and read the log:
     $INSTALL_DIR/deploy/run-daily.sh; echo "exit \$?"
     less $INSTALL_DIR/data/logs/\$(date +%F).log

7. Schedule it: crontab -e, and paste (see deploy/crontab.example):
     CRON_TZ=Europe/Oslo
     PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
     0 6 * * * $INSTALL_DIR/deploy/run-daily.sh
EOF
