FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py database.py output.py queries.py steampricingproject.py ./

RUN useradd --create-home --uid 10001 bot
USER bot

# As PID 1, Python ignores docker stop's default SIGTERM. SIGINT raises
# KeyboardInterrupt, which discord.py's Client.run handles by closing cleanly.
STOPSIGNAL SIGINT

CMD ["python", "bot.py"]
