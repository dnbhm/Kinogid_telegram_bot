FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /app/data && chown bot:bot /app/data
COPY --chown=bot:bot kinogid ./kinogid
USER bot
CMD ["python", "-m", "kinogid"]
