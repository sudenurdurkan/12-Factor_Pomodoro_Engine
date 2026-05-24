# 1. Use the official lightweight Python base image (Factor II - Dependencies)
FROM python:3.11-slim

# 2. Set the working directory inside the container
WORKDIR /app

# 3. Copy the dependency list into the container
COPY requirements.txt .

# 4. Install dependencies inside the container
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy all remaining application code (app.py etc.) into the container
COPY . .

# 6. Specify the port the application exposes externally (Factor VII - Port Binding)
EXPOSE 5000

# 7. Command to start the application
CMD ["python", "app.py"]