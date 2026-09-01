"""FastAPI HTTP server — WhatsApp channel bridge endpoint.

Receives inbound messages from whatsapp_bridge.js and dispatches them to
WhatsAppAgent.  Runs on localhost only; the Node bridge is the only client.

Start with:
    python -m nbchat.channels.whatsapp_server
or:
    uvicorn nbchat.channels.whatsapp_server:app --host 127.0.0.1 --port 8764
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from nbchat.channels.whatsapp_agent import WhatsAppAgent

_log = logging.getLogger("nbchat.whatsapp.server")

app = FastAPI(title="nbchat WhatsApp bridge", docs_url=None, redoc_url=None)

# Single agent instance — sessions are isolated by sender JID internally.
_agent = WhatsAppAgent()


class InboundMessage(BaseModel):
    jid: str    # sender WhatsApp JID, e.g. "+15551234567@s.whatsapp.net"
    text: str   # plain text content


class OutboundReply(BaseModel):
    reply: str


@app.post("/message", response_model=OutboundReply)
def handle_message(msg: InboundMessage) -> OutboundReply:
    """Process one inbound WhatsApp message and return the agent's reply."""
    if not msg.text.strip():
        raise HTTPException(status_code=400, detail="empty message")
    try:
        reply = _agent.handle(msg.jid, msg.text)
    except Exception as exc:
        _log.exception(f"agent error for {msg.jid}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    return OutboundReply(reply=reply or "(no response)")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8764, log_level="info")