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
    raise RuntimeError("Please set AIRTABLE_PAT, AIRTABLE_BASE_ID, and CLAUDE_API_KEY in your .env")

# ───── Configure logging ─────
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ───── Airtable configuration ─────
AIRTABLE_URL     = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
AIRTABLE_HEADERS = {"Authorization": f"Bearer {AIRTABLE_PAT}"}
TABLES = {
    "business": "BusinessConfig",
    "config":   "WhatsappConfig",
    "template": "WhatsAppReplyTemplate",
    "knowledge":"KnowledgeBase",
    "history":  "CustomerHistory",
    "sales":    "SalesData",
}

# ───── Flask app ─────
app = Flask(__name__)

# ───── Log routes & tools once on first request ─────
startup_logged = False

@app.before_request
def log_routes_and_tools():
    global startup_logged
    if not startup_logged:
        startup_logged = True
        # 1) List all Flask routes
        logger.debug("🚦 Flask routes:")
        for rule in app.url_map.iter_rules():
            methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
            logger.debug(f"  • endpoint={rule.endpoint!r}, path={rule.rule}, methods=[{methods}]")
        # 2) List all tool IDs
        tools = [
            "Default",
            "InfoSearch",
            "FormValidation",
            "RerouteMobile",
            "RerouteBiz",
            "RerouteWinback",
            "DropDropDrop"
        ]
        logger.debug("🧰 Available tools: %s", tools)

# ───── Helper functions ─────
def detect_language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"

def call_claude(messages: list, model: str) -> dict:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key":         CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type":      "application/json"
    }
    payload = {"model": model, "max_tokens": 1024, "messages": messages}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()

def select_tool(msg: str) -> str:
    router_prompt = """You are a Sales Manager reviewing a WhatsApp conversation. 
Decide exactly one of the following TOOLS for the SalesPerson to use next.
Do NOT craft a reply to the customer—ONLY output the tool ID in JSON.
TOOLS:
Default         – Continue conversation normally.
InfoSearch      – Search the company knowledge base.
FormValidation  – Validate a filled submission form.
RerouteMobile   – Customer only wants mobile/postpaid.
RerouteBiz      – Customer wants business/corporate plan.
RerouteWinback  – Customer is switching from another provider.
DropDropDrop    – Stop all conversation per policy.
SalesPersonKnowledge:
- Has templates & basic Unifi Fibre info.
- Does NOT know edge-case details.
Customer said: "%s"
Output:
{"TOOLS":"<tool_id>"}
""" % msg

    resp = call_claude(
        messages=[{"role": "system", "content": router_prompt}],
        model="claude-opus-4-20250514"
    )
    text = resp["choices"][0]["message"]["content"].strip()
    logger.debug("Router response: %s", text)
    try:
        return text.split('"')[1]
    except:
        logger.error("Failed to parse tool from router response")
        return "Default"

def fetch_service_config(svc_no: str) -> dict:
    logger.debug("Fetching WhatsappConfig for %s", svc_no)
    formula = f"{{WhatsappNumber}}='{svc_no}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['config']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        raise ValueError(f"No WhatsappConfig for {svc_no}")
    f = recs[0]["fields"]
    biz_list = f.get("BusinessID (from Business)", [])
    if not biz_list:
        raise ValueError("Config row missing BusinessID lookup")
    return {
        "WA_ID":           f["WA_ID"],
        "BusinessID":      biz_list[0],
        "WassengerApiKey": f["WASSENGER_API_KEY"],
        "Role":            f.get("Role",""),
    }

def fetch_business_settings(biz_id: str) -> dict:
    logger.debug("Fetching BusinessConfig for %s", biz_id)
    formula = f"{{BusinessID}}='{biz_id}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['business']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        raise ValueError(f"No BusinessConfig for {biz_id}")
    f = recs[0]["fields"]
    return {
        "DefaultLanguage": f.get("DefaultLanguage","en"),
        "ClaudePrompt":    f.get("ClaudePrompt",""),
        "ClaudeModel":     f.get("ClaudeModel","claude-opus-4-20250514"),
    }

def find_template(biz: str, wa: str) -> dict | None:
    logger.debug("Looking for template for BusinessID=%s, WA_ID=%s", biz, wa)
    formula = (
        "AND("
          "{BusinessID (from Business)}='" + biz + "',"
          "{WhatsAppConfig}='"             + wa  + "'"
        ")"
    )
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['template']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    for rec in r.json().get("records", []):
        return rec["fields"]
    return None

def find_knowledge(biz: str, msg: str, role: str):
    logger.debug("Searching KnowledgeBase for BusinessID=%s", biz)
    formula = f"{{BusinessID}}='{biz}'"
    r = requests.get(f"{AIRTABLE_URL}/{TABLES['knowledge']}",
                     headers=AIRTABLE_HEADERS,
                     params={"filterByFormula": formula})
    r.raise_for_status()
    for rec in r.json().get("records", []):
        flds = rec["fields"]
        if flds.get("Title","").lower() in msg.lower():
            scripts = flds.get("RoleScripts") or {}
            return scripts.get(role) or flds.get("DefaultScript"), \
                   (flds["ImageURL"][0]["url"] if flds.get("ImageURL") else None)
    return None, None

def validate_form(message: str) -> str:
    return "Your form looks good. We'll process your order shortly."

def send_whatsapp(phone: str, text: str, token: str):
    logger.debug("Sending WhatsApp to %s: %s", phone, text)
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Token": token, "Content-Type": "application/json"},
        json={"phone": phone, "message": text}
    )
    r.raise_for_status()

def send_image(phone: str, url: str, token: str):
    logger.debug("Sending image to %s: %s", phone, url)
    r = requests.post(
        "https://api.wassenger.com/v1/messages",
        headers={"Token": token, "Content-Type": "application/json"},
        json={"phone": phone, "message": "", "url": url}
    )
    r.raise_for_status()

def record_history(biz, wa, phone, step, hist):
    logger.debug("Recording history: biz=%s, wa=%s, phone=%s, step=%s", biz, wa, phone, step)
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['history']}",
        headers={**AIRTABLE_HEADERS, "Content-Type":"application/json"},
        json={"fields":{
            "BusinessID":     biz,
            "WhatsAppConfig": wa,
            "PhoneNumber":    phone,
            "CurrentStep":    step,
            "History":        hist
        }}
    )

def record_sales(biz, wa, phone, name, svc):
    logger.debug("Recording sales: biz=%s, wa=%s, phone=%s, svc=%s", biz, wa, phone, svc)
    requests.post(
        f"{AIRTABLE_URL}/{TABLES['sales']}",
        headers={**AIRTABLE_HEADERS, "Content-Type":"application/json"},
        json={"fields":{
            "BusinessID":     biz,
            "WhatsAppConfig": wa,
            "PhoneNumber":    phone,
            "CustomerName":   name,
            "ServiceBooked":  svc,
            "Status":         "Pending"
        }}
    )

@app.route("/", methods=["GET","POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    logger.debug("1) Received webhook")
    payload = request.get_json(force=True)
    logger.debug("   payload: %s", payload)

    if payload.get("object") == "message" and payload.get("event") == "message:in:new":
        d = payload["data"]
        if d.get("meta", {}).get("isGroup"):
            logger.debug("2) Ignoring group message")
            return jsonify(status="ignored_group"), 200

        svc_no_lookup = d["toNumber"].lstrip("+")
        cus_no_lookup = d["fromNumber"].lstrip("+")
        cus_no_raw    = d["fromNumber"]
        msg           = d["body"].strip()
        logger.debug("2) Numbers — lookup svc:%s cus:%s, raw cus:%s", svc_no_lookup, cus_no_lookup, cus_no_raw)
        logger.debug("   Message: %s", msg)

        scfg = fetch_service_config(svc_no_lookup)
        bcfg = fetch_business_settings(scfg["BusinessID"])
        logger.debug("3) Service config: %s", scfg)
        logger.debug("4) Business settings: %s", bcfg)

        lang = detect_language(msg)
        logger.debug("5) Detected language: %s", lang)

        tool = select_tool(msg)
        logger.debug("6) Selected tool: %s", tool)

        if tool == "Default":
            tpl = find_template(scfg["BusinessID"], scfg["WA_ID"])
            logger.debug("7a) Template lookup: %s", tpl)
            if tpl:
                body = tpl.get("TemplateBody", "")
                send_whatsapp(cus_no_raw, body, scfg["WassengerApiKey"])
                record_history(scfg["BusinessID"], scfg["WA_ID"], cus_no_lookup, "template", f"C:{msg}|B:{body}")
                return jsonify(status="template_sent"), 200

            script, img = find_knowledge(scfg["BusinessID"], msg, scfg["Role"])
            logger.debug("7b) Knowledge lookup -> script:%s img:%s", script, img)
            if script:
                if img:
                    send_image(cus_no_raw, img, scfg["WassengerApiKey"])
                send_whatsapp(cus_no_raw, script, scfg["WassengerApiKey"])
                record_history(scfg["BusinessID"], scfg["WA_ID"], cus_no_lookup, "knowledge", f"C:{msg}|B:{script}")
                return jsonify(status="knowledge_sent"), 200

            history = f"Customer: {msg}"
            logger.debug("7c) Calling Claude fallback")
            resp = call_claude(
                messages=[
                    {"role":"system",  "content": bcfg["ClaudePrompt"].format(history=history, user_message=msg)},
                    {"role":"user",    "content": msg}
                ],
                model=bcfg["ClaudeModel"]
            )
            reply = resp["choices"][0]["message"]["content"].strip()
            logger.debug("7c) Claude reply: %s", reply)
            send_whatsapp(cus_no_raw, reply, scfg["WassengerApiKey"])
            record_history(scfg["BusinessID"], scfg["WA_ID"], cus_no_lookup, "fallback", f"C:{msg}|B:{reply}")
            if "booking" in reply.lower() or "预约" in reply:
                record_sales(scfg["BusinessID"], scfg["WA_ID"], cus_no_lookup, "Unknown","TBD")
            return jsonify(status="ok"), 200

        elif tool == "InfoSearch":
            script, img = find_knowledge(scfg["BusinessID"], msg, scfg["Role"])
            logger.debug("8) InfoSearch -> script:%s img:%s", script, img)
            if img:
                send_image(cus_no_raw, img, scfg["WassengerApiKey"])
            send_whatsapp(cus_no_raw, script or "Sorry, I couldn't find that info.", scfg["WassengerApiKey"])
            record_history(scfg["BusinessID"], scfg["WA_ID"], cus_no_lookup, "infos_]()_
