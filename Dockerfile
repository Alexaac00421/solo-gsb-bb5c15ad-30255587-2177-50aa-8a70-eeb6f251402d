FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLAIM_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY init_db.py .

# 非 root 运行，数据卷归属于该用户
RUN useradd -r -u 10001 appuser && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8000

# 容器启动前自动初始化（幂等），随后起服务
CMD ["sh", "-c", "python init_db.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
