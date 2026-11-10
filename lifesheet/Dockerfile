FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "discord.py>=2.3,<3" pg8000
COPY lifesheet.py service.py lifesheet.json /app/
ENV LIFESHEET_SCRIPT=/app/lifesheet.py \
    LIFESHEET_STATE=/app/data/state.json \
    PYTHONUNBUFFERED=1
CMD ["python", "service.py"]
