FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISK_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY wsgi.py /app/wsgi.py
COPY scripts /app/scripts

# 镜像数据目录：compose 会把命名卷挂到这里，镜像在首次启动时自动播种
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# waitress 多线程，支撑并发写入演练
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8080", "--threads=8", "wsgi:application"]
