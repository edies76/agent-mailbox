FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION server.py stores.py collaboration.py delivery.py web_pages.py a2a_gateway.py connect.html connect-en.html connect.js dashboard.html dashboard.js AGENT_PROTOCOL.md mailbox_watch.py mailbox_listener.py mailbox_mcp.py mailbox_client.py README.md CHANGELOG.md DEPLOYMENT.md app.css workspace.html workspace.js guide.js favicon.svg use-cases.html use-cases.js version.js ./
COPY onboarding.py agent-mailbox.md ./
COPY adapters/ ./adapters/
COPY docs/ ./docs/
COPY skills/ ./skills/
COPY assets/ ./assets/
COPY brand/ ./brand/
RUN python -c "import server; from pathlib import Path; from onboarding import catalog, core_archive; catalog(); core_archive(); assert Path('/app/version.js').is_file()"
ENV MAILBOX_DB=/data/mailbox.sqlite3
VOLUME /data
EXPOSE 8795
CMD ["uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8795"]

