# Use an official lightweight Python base image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# --- MODIFIED ---
# Copy ALL files (main.py, config.py, utils.py, etc.)
# from the current directory into the container's /app directory.
COPY . .

# Command to run your bot when the container starts
CMD ["python", "main.py"]
