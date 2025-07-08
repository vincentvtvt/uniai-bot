import os
from airtable import Airtable
from flask import Flask, request, jsonify
import tools  # your module of tool implementations
from your_history_module import record_history  # wherever you keep record_history

app = Flask(__name__)

# ─── Airtable CONFIG ────────────────────────────────────────────────────────────
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
BASE_ID            = "appEcGdKhP43YXmV6"
TABLE_NAME         = "ToolFunctions"

# Initialize Airtable client
airtable_client = Airtable(BASE_ID, TABLE_NAME, AIRTABLE_API_KEY)

def load_tool_map():
    """Fetch all rows from Airtable ToolFunctions and build { Category: ToolFunction }."""
    tool_map = {}
    for rec in airtable_client.get_all():
        fields = rec.get("fields", {})
        cat    = fields.get("Category")
        fn     = fields.get("ToolFunction")
        if cat and fn:
            tool_map[cat.strip()] = fn.strip()
    return tool_map

# Load once at startup (or call inside handler if you need real-time refresh)
TOOL_MAP = load_tool_map()


# ─── FLASK WEBHOOK HANDLER ─────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.json or {}
    msg      = payload.get("message", "")
    category = payload.get("Category", "")

    # 1️⃣ Look up which function to call
    tool_name = TOOL_MAP.get(category, "DefaultTool")
    handler   = getattr(tools, tool_name, tools.DefaultTool)

    # 2️⃣ Execute the tool
    resp = handler(
        business_id = payload["BusinessID"],
        wa_id       = payload["WA_ID"],
        phone       = payload["customer_phone"],
        message     = msg,
        api_key     = payload["WassengerApiKey"]
    )

    # 3️⃣ Log it
    record_history(
        payload["BusinessID"],
        payload["WA_ID"],
        payload["customer_phone"],
        tool_name,
        f"Incoming message: {msg}"
    )

    # 4️⃣ Return whatever your handler returns
    return jsonify(resp), 200


if __name__ == "__main__":
    app.run(debug=True)
