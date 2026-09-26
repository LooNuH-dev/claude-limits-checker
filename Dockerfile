FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/bot.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sqlite3,os; sqlite3.connect(os.environ['DB_PATH']).execute('select 1')"

CMD ["python", "-m", "app.main"]
