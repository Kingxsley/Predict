FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what's needed to SERVE predictions: trained model artifacts +
# inference code + dashboard. Raw/processed data and the training scripts
# aren't needed at runtime (models/*.pkl are self-contained), which keeps
# the image small and the container stateless.
COPY src/ ./src/
COPY models/ ./models/
COPY dashboard.html ./

EXPOSE 8000
CMD ["python3", "src/api.py"]
