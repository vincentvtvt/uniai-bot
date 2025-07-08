import sys
import types

# ─── STUB MODULES TO AVOID MODULE NOT FOUND ERRORS ───────────────────────────────
# Stub out 'micropip' if missing
micropip_stub = types.ModuleType('micropip')
micropip_stub.install = lambda *args, **kwargs: None
sys.modules['micropip'] = micropip_stub

# Stub out 'airtable' module if missing, providing a dummy client
try:
    from airtable import Airtable as RealAirtable
    Airtable = RealAirtable
except ModuleNotFoundError:
    class Airtable:
        """
        Dummy Airtable client stub. All tables return empty list.
        """
        def __init__(self, base_id, table_name, api_key):
            # no-op init
            self.base_id = base_id
            self.table_name = table_name
            self.api_key = api_key
        def get_all(self):
            return []

# Stub out 'tools' module if missing
eventual_tools = sys.modules.get('tools')
if eventual_tools is None:
    tools = types.ModuleType('tools')
    def DefaultTool(*args, **kwargs):
        return {'text': 'Default response'}
    def send_whatsapp(phone, msg, key):
        # no-op send
        pass
    tools.DefaultTool = DefaultTool
    tools.send_whatsapp = send_whatsapp
    sys.modules['tools'] = tools
else:
    import tools

# Stub out 'your_history_module' if missing
if 'your_history_module' not in sys.modules:
    history_stub = types.ModuleType('your_history_module')
    history_stub.record_history = lambda *args, **kwargs: None
    history_stub.fetch_history = lambda *args, **kwargs: []
    sys.modules['your_history_module'] = history_stub

import os
from flask import Flask, request, jsonify
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

# Initialize Airtable clients (stubbed if library missing)
business_at = Airtable(AIRTABLE_BASE_ID, TABLE_BUSINESS, AIRTABLE_PAT)
wa_at       = Airtable(AIRTABLE_BASE_ID, TABLE_WA, AIRTABLE_PAT)
tools_at    = Airtable(AIRTABLE_BASE_ID, TABLE_TOOLS, AIRTABLE_PAT)
template_at = Airtable(AIRTABLE_BASE_ID, TABLE_TEMPLATES, AIRTABLE_PAT)
history_at  = Airtable(AIRTABLE_BASE_ID, TABLE_HISTORY, AIRTABLE_PAT)
sales_at    = Airtable(AIRTABLE_BASE_ID, TABLE_SALES, AIRTABLE_PAT)

# ─── LOAD CONFIG AND KB INTO MEMORY ─────────────────────────────────────────────
business_cfg = {rec['fields']['BusinessID']: rec['fields'] for rec in business_at.get_all()}
wa_cfg       = {rec['fields']['WA_ID']:        rec['fields'] for rec in wa_at.get_all()}
TOOL_MAP     = {rec['fields']['Category']: rec['fields']['ToolFunction'] for rec in tools_at.get_all()}
TEMPLATES    = {(rec['fields']['Category'], rec['fields'].get('Language', 'en')): rec['fields']['Template'] 
                for rec in template_at.get_all()}
sales_data   = [rec['fields'] for rec in sales_at.get_all()]

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
    result = {}
    try:
        result = handler(
            business_id=biz_id,
            wa_id=wa_id,
            phone=phone,
            message=msg,
            api_key=wcfg.get('WassengerApiKey'),
            history=recent_hist,
            sales_data=sales_data
        ) or {}
    except Exception as e:
        record_history(biz_id, wa_id, phone, 'ErrorHandler', str(e))
        return jsonify(error=str(e)), 500

    # 5) Format the response using templates if available
    if (cat, lang) in TEMPLATES:
        try:
            response = TEMPLATES[(cat, lang)].format(**result)
        except Exception as e:
            record_history(biz_id, wa_id, phone, 'TemplateError', str(e))
            response = result.get('text', '')
    else:
        response = result.get('text', '')

    # 6) Send via WhatsApp API helper
    tools.send_whatsapp(phone, response, wcfg.get('ApiKey') or wcfg.get('WassengerApiKey'))

    # 7) Log to history
    record_history(biz_id, wa_id, phone, tool_name, f"Category={cat}, Msg={msg}, Response={response}")

    return jsonify(status='ok'), 200

# ─── BASIC UNIT TESTS ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import unittest
    
    class TestStubs(unittest.TestCase):
        def test_micropip_importable(self):
            import micropip
            self.assertTrue(hasattr(micropip, 'install'))

        def test_airtable_stub(self):
            # Airtable.get_all should return a list
            client = Airtable('base', 'table', 'key')
            self.assertIsInstance(client.get_all(), list)

        def test_tools_stub(self):
            import tools
            result = tools.DefaultTool()
            self.assertIsInstance(result, dict)
            self.assertIn('text', result)
            # send_whatsapp should not error
            tools.send_whatsapp('123', 'msg', 'key')

        def test_history_stub(self):
            from your_history_module import record_history, fetch_history
            record_history('biz','wa','phone','step','hist')
            self.assertIsInstance(fetch_history(None,'biz','wa','phone'), list)

    unittest.main()

    # For production, run: app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
