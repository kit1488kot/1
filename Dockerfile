FROM python:3.10-slim
WORKDIR /app
RUN pip install playwright && playwright install chromium
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
