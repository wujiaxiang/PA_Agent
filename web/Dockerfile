# NOTE: pa-agent-base = python:3.11-slim + Qt/X11 libs (pre-built via
# `docker run --security-opt apparmor=unconfined python:3.11-slim apt-get ...`
# then committed).  Build step can't run apt-get because LXC AppArmor
# blocks intermediate containers — see /root/CT105-README.md "Known Pitfalls".
FROM pa-agent-base:latest

WORKDIR /app

# Layer 1: install base project dependencies (cache-friendly)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Layer 2: install web dependencies
COPY web/pyproject.toml ./web/
RUN pip install --no-cache-dir -e ./web \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Layer 3: copy the rest (changes most often)
COPY pa_agent/ ./pa_agent/
COPY prompt_engineering/ ./prompt_engineering/
COPY experience/ ./experience/
COPY config/ ./config/
COPY records/ ./records/
COPY logs/ ./logs/
COPY tools/ ./tools/
COPY scripts/ ./scripts/
COPY web/ ./web/

ENV QT_QPA_PLATFORM=offscreen

EXPOSE 8000
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
