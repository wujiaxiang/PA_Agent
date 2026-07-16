FROM python:3.11-slim

# Minimal Qt/X11 libs — needed for PyQt6 import (no GUI, headless only).
# RUN --security=insecure bypasses AppArmor in LXC-hosted Docker.
RUN --security=insecure apt-get update && apt-get install -y --no-install-recommends \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-sync1 \
    libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 \
    libegl1 libgl1 libopengl0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install base project (pa_agent core)
COPY pyproject.toml ./
COPY pa_agent/ ./pa_agent/
COPY prompt_engineering/ ./prompt_engineering/
COPY experience/ ./experience/
COPY config/ ./config/
COPY records/ ./records/
COPY logs/ ./logs/
COPY tools/ ./tools/
COPY scripts/ ./scripts/
COPY .git ./

RUN --security=insecure pip install --no-cache-dir -e . \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Install web dependencies
COPY web/pyproject.toml ./web/
RUN --security=insecure pip install --no-cache-dir -e ./web \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

ENV QT_QPA_PLATFORM=offscreen

COPY web/ ./web/

EXPOSE 8000
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
