FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY polycopy ./polycopy
COPY configs ./configs

EXPOSE 8041

CMD ["python", "-m", "polycopy.web"]
