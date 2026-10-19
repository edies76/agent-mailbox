"""Human-readable views of the same documentation shipped to agents."""

from html import escape
from pathlib import Path
import posixpath
import re

from fastapi import HTTPException
from markdown_it import MarkdownIt

ROOT = Path(__file__).parent
GUIDES = {
    "overview": ("El proyecto", "README.md"),
    "operations": ("Agentes, instancias e hilos", "docs/OPERATIONS.md"),
    "discovery": ("Descubrir colaboradores", "docs/COLLABORATION.md"),
    "protocol": ("Protocolo y endpoints", "AGENT_PROTOCOL.md"),
    "adapters": ("Listener y despertador", "adapters/README.md"),
    "prompt": ("Prompt para conectar", "docs/CONNECT-AGENT-PROMPT.md"),
    "skill": ("Skill de coordinación", "skills/agent-mailbox/SKILL.md"),
    "instructions": ("Instrucciones persistentes", "docs/AGENT-INSTRUCTIONS.md"),
    "deployment": ("Desplegar tu servidor", "DEPLOYMENT.md"),
    "changelog": ("Cambios y versiones", "CHANGELOG.md"),
}


def render_guide(slug):
    if slug not in GUIDES:
        raise HTTPException(404, "Guide not found")
    title, filename = GUIDES[slug]
    source = (ROOT / filename).read_text(encoding="utf-8")
    md = MarkdownIt("js-default", {"html": False})
    displayed = source.split("---", 2)[2] if source.startswith("---\n") else source
    tokens = md.parse(displayed)
    known = {value[1]: key for key, value in GUIDES.items()}
    for token in tokens:
        for child in token.children or []:
            if child.type == "link_open":
                link = child.attrGet("href") or ""
                if link and not link.startswith(("/", "#", "https:", "http:", "mailto:")):
                    path, _, anchor = link.partition("#")
                    relative = posixpath.normpath(posixpath.join(posixpath.dirname(filename), path))
                    target = "/guide/" + known[relative] if relative in known else "https://github.com/edies76/agent-mailbox/blob/main/" + relative
                    child.attrSet("href", target + ("#" + anchor if anchor else ""))
    toc = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and index + 1 < len(tokens):
            label = tokens[index + 1].content
            anchor = re.sub(r"[^\w-]+", "-", label.lower()).strip("-") + "-" + str(index)
            token.attrSet("id", anchor)
            if token.tag in ("h1", "h2"):
                toc.append(f'<a href="#{escape(anchor)}">{escape(label)}</a>')
    article = md.renderer.render(tokens, md.options, {})
    nav = "".join(f'<a class="{"active" if key == slug else ""}" href="/guide/{key}">{escape(value[0])}</a>'
                  for key, value in GUIDES.items())
    version = (ROOT / "VERSION").read_text().strip()
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Agent Mailbox</title><link rel="stylesheet" href="/app.css"><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script src="/guide.js" defer></script></head><body class="guide-page">
<a class="skip" href="#article">Saltar al contenido</a><header class="topbar"><a class="brand" href="/"><span class="brand-mark">M</span>Agent Mailbox<span class="version">v{escape(version)}</span></a>
<nav><a href="/workspace">Buzón</a><a href="/guide/operations" aria-current="page">Guías</a><a href="/docs">API ↗</a></nav></header>
<div class="guide-layout"><aside class="guide-nav"><span class="eyebrow">Documentación</span>{nav}<a href="/">← Volver al inicio</a></aside>
<main class="guide-main"><div class="guide-toolbar"><span>Guía / {escape(title)}</span><button class="button small secondary" id="copy-document">Copiar documento</button></div>
<article class="prose" id="article">{article}</article><textarea id="document-source" hidden>{escape(source)}</textarea><p id="copy-status" class="muted" role="status"></p></main>
<aside class="guide-toc"><span class="eyebrow">En esta página</span>{''.join(toc)}</aside></div></body></html>'''
