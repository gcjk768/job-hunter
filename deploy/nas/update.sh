#!/bin/sh
# Update the NAS job watcher from GitHub and restart it. Run over SSH from anywhere:
#   sh /volume1/docker/job-hunter/deploy/nas/update.sh
# Pulls main (fast-forward only, so local edits are never overwritten), rebuilds the image only if the
# Dockerfile changed (Docker's cache makes an unchanged build instant), and restarts the container so
# the mounted code is reloaded. Private files (.env, content.py, my_profile.py) are gitignored and untouched.
set -eu
cd "$(dirname "$0")/../.."
before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
for f in .env build/content.py build/my_profile.py; do
  [ -f "$f" ] || echo "note: $f is missing (see README > On a NAS)"
done
docker compose -f deploy/nas/compose.yaml -p job-hunter up -d --build
docker compose -f deploy/nas/compose.yaml -p job-hunter restart
echo "job-hunter: $before -> $after. Self-check:"
docker exec job-hunter python build/nas_agent.py --selfcheck || echo "(fix the ❌ lines above, then run this again)"
