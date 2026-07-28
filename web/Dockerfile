FROM python:3.11-slim

WORKDIR /app

# git needed for tvdatafeed git dep; no Qt/X11 libs — web backend doesn't import PyQt6
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Layer 1: base project (pa_agent core, no Qt)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Layer 2: tvdatafeed (git dependency, not cached by pip editable install)
RUN pip install --no-cache-dir git+https://github.com/rongardF/tvdatafeed.git \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Layer 3: web deps (pa-agent[web] pulls fastapi/uvicorn/sse-starlette)
COPY web/pyproject.toml ./web/
RUN pip install --no-cache-dir -e ./web \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Layer 4: application code
COPY pa_agent/ ./pa_agent/
COPY prompt_engineering/ ./prompt_engineering/
COPY experience/ ./experience/
COPY config/ ./config/
COPY records/ ./records/
COPY logs/ ./logs/
COPY tools/ ./tools/
COPY scripts/ ./scripts/
COPY web/ ./web/

EXPOSE 8000
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
