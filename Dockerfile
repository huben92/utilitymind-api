FROM node:22-alpine AS frontend-build
WORKDIR /workspace/app
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/index.html app/vite.config.js ./
COPY app/src ./src
RUN npm run build

FROM python:3.12-slim
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
COPY api/requirements-operations.txt api/requirements-markdown.txt ./api/
RUN pip install --no-cache-dir -r api/requirements-operations.txt -r api/requirements-markdown.txt
# Copy source files explicitly so local secrets and Git metadata never enter the image.
COPY api/*.py ./api/
COPY app/src/styles ./app/src/styles
COPY app/legacy ./app/legacy
COPY --from=frontend-build /workspace/app/dist ./app/dist
EXPOSE 8765
CMD ["python", "api/operations_app.py"]
