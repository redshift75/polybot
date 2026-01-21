# Polymarket Unusual Move Tracker

Local Streamlit app that tracks the most liquid Polymarket markets and alerts on unusual probability moves using Polymarket WebSocket data.

## Setup

1. Create a virtualenv and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Configure environment variables:
   - Copy `env.example` to `.env` and edit values.
   - Set `POLYMARKET_WS_URL` to the event WebSocket endpoint you want to use.
   - Fill SMTP settings if you want email alerts.

3. Run the app:
   ```bash
   streamlit run app.py
   ```

## Notes
- The app defaults to an event-style WebSocket URL. If your endpoint expects a different subscription payload, paste a JSON override in the sidebar.
- For CLOB price updates, set mode to `clob` and use the CLOB WebSocket URL (e.g., `wss://ws-subscriptions-clob.polymarket.com`).
