#!/usr/bin/env bash
# Set up Tingposten on the Pi (Raspberry Pi OS Trixie, 64-bit).
# Safe to run again: every step checks before it changes anything.
#
#   git clone https://github.com/v3x4t0r/tingposten.git ~/tingposten
#   ~/tingposten/deploy/setup-pi.sh
set -euo pipefail

GITHUB_REPO="v3x4t0r/tingposten"  # owner/name; may move to its own account later
CODEX_VERSION="0.164.0"            # the version the pipeline was tested with
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
# The writer runs `claude -p` on the subscription; tingposten passes .env on.
if ! grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' .env; then
  printf '\n# From `claude setup-token` (Claude subscription; see deploy/README.md)\nCLAUDE_CODE_OAUTH_TOKEN=\n' >> .env
  echo "added CLAUDE_CODE_OAUTH_TOKEN= to .env"
fi
# A non-empty API key would make `claude -p` bill the API instead of the plan.
if grep -q '^ANTHROPIC_API_KEY=.' .env; then
  echo "warning: ANTHROPIC_API_KEY is set in .env; claude -p will bill the API, not the subscription" >&2
fi
# The admin panel's password (tingposten admin; any user name in the browser).
if ! grep -q '^TINGPOSTEN_ADMIN_PASSORD=.' .env; then
  pw="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  sed -i '/^TINGPOSTEN_ADMIN_PASSORD=/d' .env
  printf '\n# Password for the admin panel (tingposten admin; any user name)\nTINGPOSTEN_ADMIN_PASSORD=%s\n' "$pw" >> .env
  echo "added a random TINGPOSTEN_ADMIN_PASSORD to .env"
fi
mkdir -p data/logs
chmod +x deploy/*.sh

step "model CLIs (claude writes, codex fact-checks)"
if ! command -v claude >/dev/null; then
  # Anthropic's native installer (Linux x64 and arm64); puts claude in ~/.local/bin.
  curl -fsSL https://claude.ai/install.sh | bash || echo "claude install failed; see deploy/README.md" >&2
fi
command -v claude >/dev/null && claude --version
if command -v codex >/dev/null; then
  codex --version
else
  echo "codex is not installed. Either (needs Node 18+):"
  echo "  sudo apt install nodejs npm && sudo npm install -g @openai/codex@$CODEX_VERSION"
  echo "or the aarch64 binary from https://github.com/openai/codex/releases/tag/rust-v$CODEX_VERSION"
  echo "  (codex-aarch64-unknown-linux-musl.tar.gz, unpack to ~/.local/bin/codex)"
fi

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
1. Fill in the contact address (TINGPOSTEN_CONTACT) in .env:
     nano $INSTALL_DIR/.env
   Leave ANTHROPIC_API_KEY and OPENAI_API_KEY empty: the models run on your
   subscriptions through the CLIs, not on API keys.

2. Log in the two model CLIs (both use your subscriptions and share their
   session limits with your own use; a limit just leaves saker for the next run):
    Claude:  claude setup-token
             (it shows a login link to open on any device; you can also run
             it on your PC) and put the long-lived token it prints in .env as
             CLAUDE_CODE_OAUTH_TOKEN=...
    Codex:   codex login --device-auth
             open the link it shows on your phone or PC and enter the code;
             check with: codex login status
    Test both:
             cd $INSTALL_DIR && set -a && . ./.env && set +a && claude -p "Svar bare: ok"
             codex exec --skip-git-repo-check -s read-only "Svar bare: ok"

3. Add the deploy key on GitHub, with "Allow write access" ticked:
     https://github.com/$GITHUB_REPO/settings/keys/new
   Key:
     $(cat "$SSH_KEY.pub")

EOF

if grep -qs "^Host $SSH_ALIAS\$" "$HOME/.ssh/config"; then
  echo "4. ~/.ssh/config already has Host $SSH_ALIAS."
else
  cat <<EOF
4. Add this to ~/.ssh/config so pushes use the deploy key:

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

5. Name the commits the Pi makes to the nettside branch:
     git -C $INSTALL_DIR config user.name "Tingposten"
     git -C $INSTALL_DIR config user.email "<address>"

6. Time zone is: $tz$( [ "$tz" = Europe/Oslo ] || printf '  <- set it: sudo timedatectl set-timezone Europe/Oslo' )

7. Publishing is OFF until you turn it on (everything stays local):
     touch $INSTALL_DIR/PUBLISER     # on
     rm $INSTALL_DIR/PUBLISER        # off again
   A STOP file still wins over PUBLISER.

8. Run once by hand and read the log:
     $INSTALL_DIR/deploy/run-daily.sh; echo "exit \$?"
     less $INSTALL_DIR/data/logs/\$(date +%F).log

9. Schedule it: crontab -e, and paste (see deploy/crontab.example):
     CRON_TZ=Europe/Oslo
     PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
     0 6 * * * $INSTALL_DIR/deploy/run-daily.sh

10. The control panel (status, when the next protokoll is due, and a button that
    downloads the PDFs, writes the cards and publishes), started with the Pi:
     sed 's|/home/<user>/tingposten|$INSTALL_DIR|g; s|/home/<user>|$HOME|g; s|<user>|$(id -un)|g' \\
       $INSTALL_DIR/deploy/tingposten-admin.service | sudo tee /etc/systemd/system/tingposten-admin.service >/dev/null
     sudo systemctl daemon-reload && sudo systemctl enable --now tingposten-admin
    Open http://$(hostname).local:8787/ (or the Pi's IP address) on your phone or PC at home.
    Any user name; the password is in .env:
     grep ADMIN_PASSORD $INSTALL_DIR/.env
    Keep port 8787 closed on the router: the panel is for the home network only.
EOF
