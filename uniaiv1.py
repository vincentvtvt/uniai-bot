import os
import re
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ───── Load & validate environment variables ─────
load_dotenv()
AIRTABLE_PAT     = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")

if not (AIRTABLE_PAT and AIRTABLE_BASE_ID and CLAUDE_API_KEY):
    raise RuntimeError("Set AIRTABLE_PAT, AIRTABLE_BASE_ID & CLAUDE_API_KEY in your .env")

# ───── Airtable configuration ─────
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
HEADERS      = {"Authorization": f"Bearer {AIRTABLE_PAT}"}
TABLES = {
    "business": "BusinessConfig",
    "config":   "WhatsappConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge":"KnowledgeBase",
    "history":  "CustomerHistory",
    "sales":    "SalesData"
}

# ───── Helpers ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def fetch_service_config(svc_no: str) -> dict:
    """
    Lookup in WhatsappConfig where {WhatsappNumber} = svc_no.
    Unpack the linked BusinessID from BusinessConfig.
    """
    formula = f"{{WhatsappNumber}}='{svc_no}'"
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['config']}",
        headers=HEADERS,
        params={"filterByFormula": formula}
    )
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if not recs:
        raise ValueError(f"No WhatsappConfig for {svc_no}")
    fields = recs[0]["fields"]

    wa_id = fields.get("WA_ID")
    token = fields.get("WASSENGER_API_KEY")
    if not (wa_id and token):
        raise ValueError("Config record missing WA_ID or WASSENGER_API_KEY")

    # This is the correct lookup column name in your table:
    biz_list = fields.get("BusinessID (from Business)", [])
    if not isinstance(biz_list, list) or not biz_list:
        raise ValueError("Config record missing BusinessID lookup")
    business_id = biz_list[0]

    return {
        "WA_ID":           wa_id,
        "BusinessID":      business_id,
        "WassengerApiKey": token
    }

def fetch_business_settings(biz_id: str) -> dict:
    """
    Lookup in BusinessConfig where {BusinessID} = biz_id.
    Returns default language + Claude settings.
    """
    formula = f"{{BusinessID}}='{biz_id}'"
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['business']}",
        headers=HEADERS,
        params={"filterByFormula": formula}
    )
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if not recs:
        raise ValueError(f"No BusinessConfig for {biz_id}")
    f = recs[0]["fields"]
    return {
        "DefaultLanguage": f.get("DefaultLanguage", "en"),
        "ClaudePrompt":    f.get("ClaudePrompt", ""),
        "ClaudeModel":     f.get("ClaudeModel", "claude-2.1")
    }

def find_template(biz: str, wa: str, msg: str) -> dict | None:
    """
    Lookup in WhatsAppReplyTemplate where
      {BusinessID (from Business)} = biz
      AND {WhatsAppConfig} = wa
    """
    formula = (
        "AND("
          "{BusinessID (from Business)}='" + biz + "',"
          "{WhatsAppConfig}='"             + wa  + "'"
        ")"
    )
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['template']}",
        headers=HEADERS,
        params={"filterByFormula": formula}
    )
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        return rec["fields"]
    return None

def find_knowledge(biz: str, msg: str, role: str):
    resp = requests.get(
        f"{AIRTABLE_URL}/{TABLES['knowledge']}",
        headers=HEADERS,
        params={"filterByFormula": f"{{BusinessID}}='{biz}'"}
    )
    resp.raise_for_status()
    for rec in resp.json().get("records", []):
        f = rec["fields"]
        title = f.get("Title", "")
        if title and title.lower() in msg.lower():
            scripts = f.get("RoleScripts") or {}
            script  = scripts.get(role) or f.get("DefaultScript")
            img     = (f["ImageURL"][0]["url"] if f.get("ImageURL") else None)
            return script, img
    return None, None

def call_claude(user_msg: str, history: str, prompt: str, model: str) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key":         CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type":      "application/json"
    }
    payload = {
        "model":      model,
        "max_tokens": 1024,
        "messages": [
            {"role":"system",  "content": prompt.format(history=history, user_message=user_msg)},
            {"role":"user",    "content": user_msg}
        ]
    }
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def send_whatsapp(phone: str, text: str, token: str):
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
        json={"phone": phone, "message": text}
    )
    r.raise_for_status()

def send_image(phone: str, url: str, token: str):
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
        json={"phone": phone, "message":"", "url": url}
    )
    r.raise_for_status()

def record_history(biz, wa, phone, step, hist):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**HEADERS, "Content-Type":"application/json"},
        json={"fields":{
            "BusinessID":      biz,
            "WhatsAppConfig":  wa,
            "PhoneNumber":     phone,
            "CurrentStep":     step,
            "History":         hist
        }}
    )

def record_sales(biz, wa, phone, name, svc):
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**HEADERS, "Content-Type":"application/json"},
        json={"fields":{
            "BusinessID":      biz,
            "WhatsAppConfig":  wa,
            "PhoneNumber":     phone,
            "CustomerName":    name,
            "ServiceBooked":   svc,
            "Status":          "Pending"
        }}
    )

# ───── Flask App & Webhook ─────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.route("/", methods=["GET","POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    payload = request.get_json(force=True)
    logging.info("Webhook payload: %s", payload)

    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        d = payload["data"]
        if d.get("meta", {}).get("isGroup"):
            return jsonify(status="ignored_group"), 200

        svc_no = d["toNumber"].lstrip("+")
        cus_no = d["fromNumber"].lstrip("+")
        msg    = d["body"].strip()

        scfg = fetch_service_config(svc_no)
        biz  = scfg["BusinessID"]
        wa   = scfg["WA_ID"]
        key  = scfg["WassengerApiKey"]

        bcfg = fetch_business_settings(biz)
        lang = bcfg["DefaultLanguage"]

        # 1) Template
        tpl = find_template(biz, wa, msg)
        if tpl:
            body = tpl.get("TemplateBody", "")
            send_whatsapp(cus_no, body, key)
            record_history(biz, wa, cus_no, "template", f"C:{msg}|B:{body}")
            return jsonify(status="template_sent"), 200

        # 2) Knowledge
        script, img = find_knowledge(biz, msg, scfg.get("Role", ""))
        if script:
            if img:
                send_image(cus_no, img, key)
            send_whatsapp(cus_no, script, key)
            record_history(biz, wa, cus_no, "knowledge", f"C:{msg}|B:{script}")
            return jsonify(status="knowledge_sent"), 200

        # 3) Claude fallback
        hist  = f"Customer: {msg}"
        reply = call_claude(msg, hist, bcfg["ClaudePrompt"], bcfg["ClaudeModel"])
        send_whatsapp(cus_no, reply, key)
        record_history(biz, wa, cus_no, "fallback", f"C:{msg}|B:{reply}")

        if "booking" in reply.lower() or "预约" in reply:
            record_sales(biz, wa, cus_no, "Unknown", "TBD")

        return jsonify(status="ok"), 200

    return jsonify(status="ignored"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
