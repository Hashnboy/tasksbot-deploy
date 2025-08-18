FROM python:3.10-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends     build-essential curl ca-certificates   && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY app/ /app/

ENV PORT=5000
EXPOSE 5000

CMD ["gunicorn","--workers","1","--threads","4","--bind","0.0.0.0:5000","tasks_bot:app"]
