FROM python:3.11-slim

WORKDIR /app

# Отключаем буферизацию вывода Python для мгновенного логирования в Coolify
ENV PYTHONUNBUFFERED=1

COPY claude_checker.py /app/claude_checker.py

RUN chmod +x /app/claude_checker.py

# Healthcheck для Coolify
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import sys; sys.exit(0)"

CMD ["python3", "claude_checker.py", "daemon"]
