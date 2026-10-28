FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

COPY bot/requirements.txt /app/bot/
RUN pip install --no-cache-dir -r /app/bot/requirements.txt \
    && playwright install --with-deps chromium

COPY bot/bot.py bot/summarize.py /app/bot/
COPY extension/meet-core.js /app/extension/

WORKDIR /app/bot
ENTRYPOINT ["xvfb-run", "-a", "python", "bot.py"]
