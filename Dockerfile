FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/bot.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,time; p='/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 3*int(os.environ.get('CHECK_INTERVAL','300'))+60 else 1)"

CMD ["python", "-m", "app.main"]
