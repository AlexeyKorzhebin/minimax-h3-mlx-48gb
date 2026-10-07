FROM python:3.12-slim
# spec §3.2: the panel only -- no CUDA, no mlx/mlx-vlm/opencv/scipy. ffmpeg for assembly, the
# track pieces and the LTX pad/mux; node for the front-end checks the test suite runs; tzdata so
# the compose TZ takes effect (container clock = the owner's browser clock).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-panel.txt /app/
RUN pip install --no-cache-dir -r requirements-panel.txt
# owned by the app user: tests/test_markers.py writes probe modules next to the tests
COPY --chown=1000:1000 . /app
# Editable on purpose: provider.system_prompt() and web.REPO_ROOT read files next to the sources.
RUN pip install --no-cache-dir --no-deps -e .
RUN chmod 0755 /app/docker/entrypoint.sh
ENV H3_ENGINE=sglang PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
