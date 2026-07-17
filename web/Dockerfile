FROM python:3.11-slim

WORKDIR /app

# Install base project (pa_agent core)
COPY pyproject.toml ./
COPY pa_agent/ ./pa_agent/
COPY prompt_engineering/ ./prompt_engineering/
COPY config/ ./config/
COPY logs/ ./logs/
COPY tools/ ./tools/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e . \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Install web dependencies
COPY web/pyproject.toml ./web/
RUN pip install --no-cache-dir -e ./web \
    -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

COPY web/ ./web/

EXPOSE 8000
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
