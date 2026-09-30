# Baked image for the NAS job watcher: Python + python-docx + the Claude Code CLI (`claude -p` is the
# fit judge). Everything is installed at build time, so a restart with no internet (NAS boot before
# the router, ISP blip) never crash-loops on an install step. The code stays on the share (mounted
# at /app), so a code update needs only a restart; rebuild when this file changes or to update claude.
FROM python:3.12-slim-bookworm
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && pip install --no-cache-dir python-docx \
 && claude --version
WORKDIR /app
# Pinned by the image: updates come from a rebuild, not from the CLI rewriting itself at runtime.
ENV PYTHONUNBUFFERED=1 DISABLE_AUTOUPDATER=1
CMD ["python", "-u", "build/nas_agent.py"]
