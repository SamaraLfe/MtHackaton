FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY requirements-docs.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements-docs.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
