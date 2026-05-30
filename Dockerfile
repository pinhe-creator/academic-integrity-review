FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

EXPOSE 8000
# 有平台注入的 $PORT 就用它，否则默认 8000（本地与 PaaS 两兼容）
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
