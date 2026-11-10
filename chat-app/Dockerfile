FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

RUN useradd --uid 10001 --no-create-home appuser
USER 10001

EXPOSE 8080
# 0.0.0.0 = the Pod network inside the cluster. Nothing is published from the container;
# exposure is controlled by the ClusterIP Service (internal only).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
