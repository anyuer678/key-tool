FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY providers.example.json ./

# 运行时数据（SQLite）与密钥配置
VOLUME ["/app/data"]

ENV KEYTOOL_ADMIN_TOKEN=change-me \
    KEYTOOL_SECRET_KEY=""

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
