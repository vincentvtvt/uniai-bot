```python
import os
from flask import Flask, request, jsonify
from airtable import Airtable
import tools  # your module of tool functions
from your_history_module import record_history, fetch_history

# ─── AIRTABLE CONFIG ────────────────────────────────────────────────────────────
AIRTABLE_PAT     = os.getenv("AIRTABLE_PAT")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# Table names in your Airtable base
TABLE_BUSINESS   = "BusinessConfig"
TABLE_WA         = "WhatsappConfig"
TABLE_TOOLS      = "KnowledgeBase"
TABLE_TEMPLATES  = "WhatsAppReplyTemplate"
TABLE_HISTORY    = "CustomerHistoryTable"
TABLE_SALES      = "SalesData"

# Initialize Airtable clients
business_at = Airtable(AIRTABLE_BASE_ID, TABLE_BUSINESS, AIRTABLE_PAT)
wa_at       = Airtable(AIRTABLE_BASE_ID, TABLE_WA, AIRTABLE_PAT)
tools_at    = Airtable(AIRTABLE_BASE_ID, TABLE_TOOLS, AIRTABLE_PAT)
template_at = Airtable(AIRTABLE_BASE_ID, TABLE_TEMPLATES, AIRTABLE_PAT)
history_at  = Airtable(AIRTABLE_BASE_ID, TABLE_HISTORY, AIRTABLE_PAT)
sales_at    = Airtable(AIRTABLE_BASE_ID, TABLE_SALES, AIRTABLE_PAT)

# ─── LOAD CONFIG AND KB INTO MEMORY ─────────────────────────────────────────────
# Business and WhatsApp configuration as dicts keyed by ID
business_cfg = {rec['fields']['BusinessID']: rec['fields'] for rec in business_at.get_all()}
wa_cfg       = {rec['fields']['WA_ID']:        rec['fields'] for rec in wa_at.get_all()}

# Knowledge base: Category → ToolFunction mapping
TOOL_MAP = {rec['fields']['Category']: rec['fields']['ToolFunction'] for rec in tools_at.get_all()}

# Reply templates keyed by (Category, Language)
TEMPLATES = {
    (rec['fields']['Category'], rec['fields'].get('Language', 'en')): rec['fields']['Template']
    for rec in template_at.get_all()
}

# Sales data loaded into memory for quick lookup
sales_data = [rec['fields'] for rec in sales_at.get_all()]

# ─── FLASK SETUP ────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.get_json(force=True)
    biz_id  = payload.get('BusinessID')
    wa_id   = payload.get('WA_ID')
    phone   = payload.get('customer_phone')
    msg     = payload.get('message', '')
    cat     = payload.get('Category', '')
    lang    = payload.get('Language', 'en')

    # 1) Lookup configs from Airtable
    scfg = business_cfg.get(biz_id, {})
    wcfg = wa_cfg.get(wa_id, {})

    # 2) Fetch recent history for context
    recent_hist = fetch_history(history_at, biz_id, wa_id, phone)

    # 3) Determine the tool to dispatch via knowledge base
    tool_name = TOOL_MAP.get(cat, 'DefaultTool')
    handler   = getattr(tools, tool_name, tools.DefaultTool)

    # 4) Execute the tool function
    result = handler(
        business_id = biz_id,
        wa_id       = wa_id,
        phone       = phone,
        message     = msg,
        api_key     = wcfg.get('WassengerApiKey'),
        history     = recent_hist,
        sales_data  = sales_data
    )

    # 5) Format the response using templates if available
    if (cat, lang) in TEMPLATES:
        template = TEMPLATES[(cat, lang)]
        response = template.format(**result)
    else:
        response = result.get('text', '')

    # 6) Send via WhatsApp API helper
    tools.send_whatsapp(phone, response, wcfg.get('ApiKey') or wcfg.get('WassengerApiKey'))

    # 7) Log to history
    record_history(
        biz_id,
        wa_id,
        phone,
        tool_name,
        f"Category={cat}, Msg={msg}, Response={response}"
    )

    return jsonify(status='ok'), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
```
