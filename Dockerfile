FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# What's needed to SERVE predictions: trained model artifacts + inference
# code + dashboard, plus the processed (cleaned) historical data — used at
# request time for real head-to-head/recent-form rationale, not for
# training. Raw source CSVs and the training scripts aren't needed at
# runtime and stay out via .dockerignore; the processed parquet files are
# ~10MB combined, small enough to keep the image lean.
COPY src/ ./src/
COPY models/ ./models/
COPY data/ ./data/
COPY reports/ ./reports/
COPY web/ ./web/

EXPOSE 8000
CMD ["python3", "src/api.py"]
