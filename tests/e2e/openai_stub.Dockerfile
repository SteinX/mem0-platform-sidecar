FROM python:3.12-slim

COPY openai_stub.py /openai_stub.py

CMD ["python", "/openai_stub.py"]
