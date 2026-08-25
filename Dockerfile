FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY bot.sh .
RUN chmod +x bot.sh

ENTRYPOINT ["./bot.sh"]
