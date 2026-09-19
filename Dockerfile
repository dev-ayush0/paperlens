FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN python3 -c "import base64,gzip,pathlib; pathlib.Path('server.py').write_bytes(gzip.decompress(base64.b64decode(pathlib.Path('payload/server.b64').read_text()))); pathlib.Path('public').mkdir(exist_ok=True); pathlib.Path('public/index.html').write_bytes(gzip.decompress(base64.b64decode(pathlib.Path('payload/index.b64').read_text())))"
RUN rm -rf payload
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "server.py"]
