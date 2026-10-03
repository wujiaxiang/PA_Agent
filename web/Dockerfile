FROM python:3.11-slim

WORKDIR /app

# git needed for tvdatafeed git dep; no Qt/X11 libs — web backend doesn't import PyQt6
# fonts-noto-cjk: trade_logger renders the trade chart with Chinese labels; without
# a CJK font every glyph renders as tofu boxes in the image we attach to Feishu.
RUN apt-get update && apt-get install -y --no-install-recommends git fonts-noto-cjk && \
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
# Only the example templates — copying the whole config/ baked config/settings.json
# (API key, Feishu/PushPlus/Tushare/TradingView credentials) into an image layer.
# Real config arrives via the bind mount in docker-compose.
RUN mkdir -p config && cp config/*.example.json config/ 2>/dev/null || true
# trade_records/ holds the trade CSV + chart PNGs written after each order signal.
# It must exist in the image and be mounted, otherwise the trade log is ephemeral
# and re-running the container silently loses it.
RUN mkdir -p records logs trade_records
COPY records/ ./records/
COPY logs/ ./logs/
COPY tools/ ./tools/
COPY scripts/ ./scripts/
COPY web/ ./web/

EXPOSE 8000
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
