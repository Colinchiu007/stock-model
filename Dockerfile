# 语法说明: 见 docs/OPERATIONS.md §7
# 构建时可选代理(中国网络): --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://pypi.org/simple

# TZ 数据(A股调度需要 Asia/Shanghai) + curl(健康检查)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖(利用 Docker 层缓存: 代码改动不触发重装)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[web,schedule,quant]"

# 再拷其余源码与脚本(改动只需重建薄层)
COPY src ./src
COPY scripts ./scripts
COPY data/paper/holidays.json ./data/paper/holidays.json
COPY experiments ./experiments

# 非-root 运行
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 单 worker 硬约束: 绝不加 --workers(每进程一份调度器会让同一账户被重复推进)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "uvicorn", "stock_model.web.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
