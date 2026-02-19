FROM python:3.10-slim

# Create a non-root user (Hugging Face standard)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Install dependencies
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy project files
COPY --chown=user . .

# Expose port (HF looks for 7860)
EXPOSE 7860

# Run the polling bot
CMD ["python", "bot_polling.py"]
