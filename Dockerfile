FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION server.py stores.py collaboration.py delivery.py web_pages.py connect.html connect.js dashboard.html dashboard.js AGENT_PROTOCOL.md mailbox_watch.py mailbox_mcp.py mailbox_client.py README.md CHANGELOG.md DEPLOYMENT.md app.css workspace.html workspace.js guide.js favicon.svg use-cases.html use-cases.js ./
COPY adapters/ ./adapters/
COPY docs/ ./docs/
COPY skills/ ./skills/
ENV MAILBOX_DB=/data/mailbox.sqlite3
VOLUME /data
EXPOSE 8795
CMD ["uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8795"]

