# We updated the version here to match the newer pip library
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

COPY . /app

# Upgrade pip and install dependencies
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

CMD ["python", "main.py"]
