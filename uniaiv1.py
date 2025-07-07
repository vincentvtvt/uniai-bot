import os
import re
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ───── Load & validate env vars ─────
load_dotenv()
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")

if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and CLAUDE_API_KEY):
    raise RuntimeError("Missing one of AIRTABLE_API_KEY, AIRTABLE_BASE_ID, or CLAUDE_API_KEY")

# ───── Airtable setup ─────
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS      = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
TABLES = {
    "config":   "WhatsappConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge":"KnowledgeBase",
    "history":  "CustomerHistory",
    "sales":    "SalesData"
}

# ───── Helpers ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def fetch_whatsapp_config(service_number: str) -> dict:
    """
    Lookup WhatsappConfig by the numeric service number (no '+').
    Handles numeric vs text field comparisons.
    """
    # assume service_number is digits only
    if service_number.isdigit():
        formula = f"{{WhatsappNumber}}={service_number}"
    else:
        formula = f"{{WhatsappNumber}}='{service_number}'"
    params = {"filterByFormula": formula}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}",
                        headers=HEADERS, params=params)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        raise ValueError(f"No config for service {service_number}")
    rec = records[0]
    flds = rec["fields"]
    return {
        "WA_ID":            flds["WA_ID"],
        "Business":         flds["Business"],
        "WassengerApiKey":  flds.get("WASSENGER_API_KEY")
    }

def find_template(business_id: str, wa_id: str, msg: str) -> dict | None:
    """
    Find a matching template row by Business + WhatsAppConfig.
    """
    formula = (
        f"AND("
          f"{{Business}}='{business_id}',"
          f"{{WhatsAppConfig}}='{wa_id}'"
        f")"
    )
    params = {"filterByFormula": formula}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}",
                        headers=HEADERS, params=params)
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        return rec["fields"]
    return None

def find_knowledge(business_id: str, msg: str, role: str):
    params = {"filterByFormula": f"{{Business}}='{business_id}'"}
    resp = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}",
                        headers=HEADERS, params=params)
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        flds = rec["fields"]
        title = flds.get("Title","")
        if title and title.lower() in msg.lower():
            scripts = flds.get("RoleScripts") or {}
            script = scripts.get(role) or flds.get("DefaultScript")
            img = None
            if flds.get("ImageURL"):
                img = flds["ImageURL"][0]["url"]
            return script, img
    return None, None

def call_claude(user_msg: str, history: str, prompt: str, model: str) -> str:
    """
    Chat via Anthropic’s /v1/messages API.
    """
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {"role":"system", "content": prompt.format(history=history, user_message=user_msg)},
            {"role":"user",   "content": user_msg}
        ]
    }
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def send_whatsapp(phone: str, text: str, api_key: str):
    url = "https://api.wassenger.com/v1/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"}
    data = {"phone": phone, "message": text}
    resp = requests.post(url, headers=headers, json=data)
    resp.raise_for_status()

def send_image(phone: str, image_url: str, api_key: str):
    url = "https://api.wassenger.com/v1/messages"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"}
    data = {"phone": phone, "message":"", "url": image_url}
    resp = requests.post(url, headers=headers, json=data)
    resp.raise_for_status()

def record_history(biz: str, cfg_id: str, phone: str, step: str, hist: str):
    payload = {"fields": {
        "Business": biz,
        "WhatsAppConfig": cfg_id,
        "PhoneNumber": phone,
        "CurrentStep": step,
        "History": hist
    }}
    requests.post(f"{AIRTABLE_URL}/{TABLES['history']}",
                  headers={**HEADERS,"Content-Type":"application/json"},
                  json=payload)

def record_sales(biz: str, cfg_id: str, phone: str, name: str, svc: str, status="Pending"):
    payload = {"fields": {
        "Business": biz,
        "WhatsAppConfig": cfg_id,
        "PhoneNumber": phone,
        "CustomerName": name,
        "ServiceBooked": svc,
        "Status": status
    }}
    requests.post(f"{AIRTABLE_URL}/{TABLES['sales']}",
                  headers={**HEADERS,"Content-Type":"application/json"},
                  json=payload)

# ───── Flask App ─────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.route("/", methods=["GET","POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", payload)

    # Wassenger v1 inbound
    if payload.get("object")=="message" and payload.get("event")=="message:in:new":
        data = payload["data"]
        # ignore group chats
        if data.get("meta",{}).get("isGroup"):
            return jsonify({"status":"ignored_group"}), 200

        service_no  = data.get("toNumber","").lstrip("+")
        customer_no = data.get("fromNumber","").lstrip("+")
        msg         = data.get("body","").strip()

        cfg = fetch_whatsapp_config(service_no)
        biz = cfg["Business"]
        wa  = cfg["WA_ID"]
        key = cfg["WassengerApiKey"]

        # 1) Template
        tpl = find_template(biz, wa, msg)
        if tpl:
            # assumes you have a TemplateBody field in your table
            body = tpl.get("TemplateBody","")
            send_whatsapp(customer_no, body, key)
            record_history(biz, wa, customer_no, "template", f"C: {msg} | B: {body}")
            return jsonify({"status":"template_sent"}), 200

        # 2) Knowledge
        script, img = find_knowledge(biz, msg, cfg.get("Role",""))
        if script:
            if img:
                send_image(customer_no, img, key)
            send_whatsapp(customer_no, script, key)
            record_history(biz, wa, customer_no, "knowledge", f"C: {msg} | B: {script}")
            return jsonify({"status":"knowledge_sent"}), 200

        # 3) Fallback to Claude
        hist = f"Customer: {msg}"
        reply = call_claude(msg, hist, cfg.get("ClaudePrompt",""), cfg.get("ClaudeModel",""))
        send_whatsapp(customer_no, reply, key)
        record_history(biz, wa, customer_no, "fallback", f"C: {msg} | B: {reply}")
        if "booking" in reply.lower() or "预约" in reply:
            record_sales(biz, wa, customer_no, "Unknown","TBD")
        return jsonify({"status":"ok"}), 200

    return jsonify({"status":"ignored"}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
