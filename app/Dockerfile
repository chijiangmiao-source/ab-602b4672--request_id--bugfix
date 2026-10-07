FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY packager ./packager

ENV DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "packager.server:app", "--host", "0.0.0.0", "--port", "8000"]
