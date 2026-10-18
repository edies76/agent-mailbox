FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION server.py stores.py connect.html connect.js dashboard.html dashboard.js AGENT_PROTOCOL.md mailbox_watch.py mailbox_mcp.py ./
COPY adapters/README.md ./adapters/README.md
COPY docs/CONNECT-AGENT-PROMPT.md ./docs/CONNECT-AGENT-PROMPT.md
COPY skills/agent-mailbox/SKILL.md ./skills/agent-mailbox/SKILL.md
ENV MAILBOX_DB=/data/mailbox.sqlite3
VOLUME /data
EXPOSE 8795
CMD ["uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8795"]

