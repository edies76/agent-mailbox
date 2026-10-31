FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION server.py stores.py collaboration.py delivery.py web_pages.py a2a_gateway.py connect.html connect-en.html connect.js dashboard.html dashboard.js message.html message.js profile.html profile.js profile-edit.js account.html account.js account-edit.js account-members.js profiles.html profiles.js join-workspace.html join-workspace.js join-member.html join-member.js AGENT_PROTOCOL.md mailbox_watch.py mailbox_listener.py mailbox_mcp.py mailbox_client.py README.md CHANGELOG.md DEPLOYMENT.md app.css workspace.html workspace.js guide.js favicon.svg use-cases.html use-cases.js version.js ./
COPY onboarding.py agent-mailbox.md ./
COPY chat.html chat.js chat.css ./
COPY adapters/ ./adapters/
COPY docs/ ./docs/
COPY skills/ ./skills/
COPY assets/ ./assets/
COPY brand/ ./brand/
RUN python -c "import sys, server; from pathlib import Path; from onboarding import catalog, core_archive; catalog(); core_archive(); missing = [name for name in ('version.js', 'app.css', 'dashboard.html', 'dashboard.js', 'message.html', 'message.js', 'profile.html', 'profiles.html', 'account.html', 'workspace.html', 'guide.js', 'favicon.svg') if not Path('/app', name).is_file()]; sys.exit('missing static file in the image: ' + ', '.join(missing)) if missing else None"
ENV MAILBOX_DB=/data/mailbox.sqlite3
VOLUME /data
EXPOSE 8795
CMD ["uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8795"]

