FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN python3 -c "import base64,gzip,pathlib; p=pathlib.Path('public/index.html'); p.write_bytes(gzip.decompress(base64.b64decode(p.read_text())))"
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "server.py"]
