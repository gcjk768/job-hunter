# Baked image for the NAS job watcher: dependencies are installed once at build time, so a restart
# with no internet (NAS boot before the router, ISP blip) no longer crash-loops on `pip install`.
# The code itself stays on the share (mounted at /app), so a code update needs only a restart.
FROM python:3.12-slim
RUN pip install --no-cache-dir python-docx
WORKDIR /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "build/nas_agent.py"]
