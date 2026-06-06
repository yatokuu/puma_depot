FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
ENV DB_PATH=/data/inventory.db
CMD ["python", "bot.py"]
