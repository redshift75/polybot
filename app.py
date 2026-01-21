import json
import os
import smtplib
import threading
import time
from urllib.parse import urlparse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import websocket
from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_ms_to_iso(value):
    try:
        ts = float(value) / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return now_utc_iso()


def normalize_market_record(market: dict, volume_override: float | None = None):
    question = market.get("question") or market.get("title") or "Unknown market"
    condition_id = market.get("conditionId") or market.get("condition_id")
    market_id = market.get("id") or market.get("marketId")
    clob_token_ids = market.get("clobTokenIds") or []
    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except json.JSONDecodeError:
            clob_token_ids = []
    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = []
    if not isinstance(outcomes, list):
        outcomes = []

    asset_ids = []
    if isinstance(clob_token_ids, list):
        asset_ids = [str(token) for token in clob_token_ids]

    yes_asset_id = None
    no_asset_id = None
    if outcomes and asset_ids and len(outcomes) == len(asset_ids):
        for outcome, asset in zip(outcomes, asset_ids):
            if str(outcome).lower() == "yes":
                yes_asset_id = asset
            elif str(outcome).lower() == "no":
                no_asset_id = asset
    if yes_asset_id is None and asset_ids:
        yes_asset_id = asset_ids[0]
    if no_asset_id is None and len(asset_ids) > 1:
        no_asset_id = asset_ids[1]
    volume = (
        volume_override
        if volume_override is not None
        else safe_float(market.get("volume24hr") or market.get("volume")) or 0.0
    )
    return {
        "market_id": str(market_id) if market_id is not None else None,
        "condition_id": str(condition_id) if condition_id is not None else None,
        "asset_id": yes_asset_id,
        "yes_asset_id": yes_asset_id,
        "no_asset_id": no_asset_id,
        "question": question,
        "volume24hr": volume,
    }


@st.cache_data(ttl=120)
def fetch_top_markets(limit: int, min_volume: float):
    resp = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "sort": "volume24hr:desc",
            "volume_num_min": min_volume,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    markets = []
    for market in data:
        markets.append(normalize_market_record(market))
    return markets


@st.cache_data(ttl=300)
def fetch_tags(limit: int = 100):
    resp = requests.get(
        "https://gamma-api.polymarket.com/tags", params={"limit": limit}, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("tags") or data.get("data") or []
    tags = []
    for tag in data:
        tag_id = tag.get("id")
        name = tag.get("label") or tag.get("name") or tag.get("title") or tag.get("slug")
        if tag_id is not None and name:
            tags.append({"id": str(tag_id), "name": name})
    return tags


@st.cache_data(ttl=120)
def fetch_events_by_tag(tag_id: str, limit: int = 200):
    resp = requests.get(
        "https://gamma-api.polymarket.com/events",
        params={"tag_id": tag_id, "active": "true", "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or []
    event_markets = []
    for event in data:
        for market in event.get("markets", []) or []:
            event_markets.append(normalize_market_record(market, volume_override=0.0))
    return event_markets


def build_subscription_payload(mode: str, markets: list[dict]):
    market_ids = [m["market_id"] for m in markets if m.get("market_id")]
    asset_ids = []
    for m in markets:
        for key in ("yes_asset_id", "no_asset_id", "asset_id"):
            asset_id = m.get(key)
            if asset_id:
                asset_ids.append(asset_id)
    asset_ids = list(dict.fromkeys(asset_ids))
    if mode == "event":
        return {"type": "subscribe", "channels": ["market"], "market_ids": market_ids}
    return {"type": "subscribe", "channel": "market", "assets_ids": asset_ids}


def normalize_ws_url(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    if not parsed.path or parsed.path == "/":
        return ws_url.rstrip("/") + "/ws/market"
    return ws_url


def extract_probability_updates(message: dict, price_source: str):
    updates = []

    if message.get("event_type") != "price_change":
        return updates

    for change in message.get("price_changes", []):
        asset_id = change.get("asset_id") or change.get("assetId")
        if price_source == "best_ask":
            prob = safe_float(change.get("best_ask") or change.get("bestAsk"))
        else:
            prob = safe_float(change.get("best_bid") or change.get("bestBid"))
        if asset_id is not None and prob is not None:
            updates.append((str(asset_id), prob))

    return updates


def summarize_event(message: dict, label_map: dict, outcome_map: dict):
    event_type = message.get("event_type") or message.get("type")
    if not event_type:
        return None
    market_id = message.get("market") or message.get("market_id") or message.get("marketId")
    asset_id = message.get("asset_id") or message.get("assetId")
    outcome = outcome_map.get(asset_id, "n/a")
    label = None
    if market_id:
        label = label_map.get(market_id)
    if label is None and asset_id:
        label = label_map.get(asset_id)
    if label is None:
        label = market_id or asset_id
    details = {}

    if event_type == "price_change":
        changes = message.get("price_changes") or []
        if changes:
            change = changes[0]
            details["best_bid"] = change.get("best_bid") or change.get("bestBid")
            details["best_ask"] = change.get("best_ask") or change.get("bestAsk")
            details["side"] = change.get("side")
            details["price"] = change.get("price")
            details["size"] = change.get("size")
    elif event_type == "last_trade_price":
        details["price"] = message.get("price")
        details["size"] = message.get("size")
        details["side"] = message.get("side")
    elif event_type == "book":
        bids = message.get("bids") or message.get("buys") or []
        asks = message.get("asks") or message.get("sells") or []
        if bids:
            details["best_bid"] = bids[0].get("price")
        if asks:
            details["best_ask"] = asks[0].get("price")
    elif event_type == "best_bid_ask":
        details["best_bid"] = message.get("best_bid") or message.get("bestBid")
        details["best_ask"] = message.get("best_ask") or message.get("bestAsk")

    details_clean = {k: v for k, v in details.items() if v is not None}
    return {
        "time": timestamp_ms_to_iso(message.get("timestamp")),
        "event": event_type,
        "market": label or "n/a",
        "outcome": outcome,
        "best_bid": details_clean.get("best_bid"),
        "best_ask": details_clean.get("best_ask"),
        "side": details_clean.get("side"),
        "price": details_clean.get("price"),
        "size": details_clean.get("size"),
    }


@dataclass
class SMTPConfig:
    enabled: bool
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str
    use_tls: bool

    @property
    def ready(self) -> bool:
        return bool(self.host and self.port and self.sender and self.recipient)


@dataclass
class MonitorState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    prices: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    alerts: deque = field(default_factory=lambda: deque(maxlen=200))
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    last_alert_at: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)


class MarketMonitor:
    def __init__(self, state: MonitorState, ws_url: str, subscription_payload: dict):
        self.state = state
        self.ws_url = ws_url
        self.subscription_payload = subscription_payload
        self.stop_event = threading.Event()
        self.thread = None
        self.ws = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _run(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self._connect_and_listen()
                backoff = 1
            except Exception as exc:
                with self.state.lock:
                    self.state.status["last_error"] = str(exc)
                    self.state.status["connected"] = False
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _connect_and_listen(self):
        def on_open(ws):
            with self.state.lock:
                self.state.status["connected"] = True
                self.state.status["last_error"] = None
                self.state.status["last_message_at"] = None
            ws.send(json.dumps(self.subscription_payload))

        def on_close(ws, *args):
            with self.state.lock:
                self.state.status["connected"] = False

        def on_error(ws, error):
            with self.state.lock:
                self.state.status["last_error"] = str(error)
                self.state.status["connected"] = False

        def on_message(ws, message):
            if message == "PING":
                ws.send("PONG")
                return
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return

            messages = data if isinstance(data, list) else [data]
            for item in messages:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "ping":
                    ws.send(json.dumps({"type": "pong"}))
                    continue
                with self.state.lock:
                    cfg = dict(self.state.config)
                event_entry = summarize_event(
                    item, cfg.get("label_map", {}), cfg.get("outcome_map", {})
                )
                if event_entry:
                    with self.state.lock:
                        self.state.events.appendleft(event_entry)
                updates = extract_probability_updates(
                    item, cfg.get("price_source", "best_bid")
                )
                if not updates:
                    continue
                ts = time.time()
                with self.state.lock:
                    self.state.status["last_message_at"] = now_utc_iso()
                for key, prob in updates:
                    self._process_update(key, prob, ts, cfg)

        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=on_open,
            on_close=on_close,
            on_error=on_error,
            on_message=on_message,
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def _process_update(self, key, prob, ts, cfg):
        threshold = cfg.get("threshold", 0.05)
        window_seconds = cfg.get("window_seconds", 600)
        cooldown_seconds = cfg.get("cooldown_seconds", 300)
        smtp_cfg = cfg.get("smtp_config")
        email_enabled = cfg.get("email_enabled", False)
        label = cfg.get("label_map", {}).get(key, key)

        with self.state.lock:
            history = self.state.history.setdefault(key, deque())
            history.append((ts, prob))
            cutoff = ts - window_seconds
            while history and history[0][0] < cutoff:
                history.popleft()
            self.state.prices[key] = prob

            if len(history) < 2:
                return
            old_prob = history[0][1]
            delta = abs(prob - old_prob)
            last_alert = self.state.last_alert_at.get(key, 0)

            if delta < threshold or (ts - last_alert) < cooldown_seconds:
                return
            alert = {
                "time": now_utc_iso(),
                "market": label,
                "market_id": key,
                "from": old_prob,
                "to": prob,
                "delta": delta,
            }
            self.state.alerts.appendleft(alert)
            self.state.last_alert_at[key] = ts

        if email_enabled and smtp_cfg and smtp_cfg.ready:
            send_email_alert(alert, smtp_cfg)


def send_email_alert(alert: dict, smtp_cfg: SMTPConfig):
    msg = EmailMessage()
    msg["Subject"] = f"Polymarket alert: {alert['market']} moved {alert['delta']:.2%}"
    msg["From"] = smtp_cfg.sender
    msg["To"] = smtp_cfg.recipient
    msg.set_content(
        "\n".join(
            [
                "Unusual probability move detected.",
                f"Market: {alert['market']}",
                f"Market ID: {alert['market_id']}",
                f"Move: {alert['from']:.4f} -> {alert['to']:.4f}",
                f"Delta: {alert['delta']:.2%}",
                f"Time (UTC): {alert['time']}",
            ]
        )
    )
    try:
        with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=10) as server:
            if smtp_cfg.use_tls:
                server.starttls()
            if smtp_cfg.user:
                server.login(smtp_cfg.user, smtp_cfg.password)
            server.send_message(msg)
    except Exception:
        pass


def get_smtp_config() -> SMTPConfig:
    return SMTPConfig(
        enabled=env_bool("SMTP_ENABLED", False),
        host=os.getenv("SMTP_HOST", ""),
        port=int(os.getenv("SMTP_PORT", "587")),
        user=os.getenv("SMTP_USER", ""),
        password=os.getenv("SMTP_PASSWORD", ""),
        sender=os.getenv("SMTP_FROM", ""),
        recipient=os.getenv("SMTP_TO", ""),
        use_tls=env_bool("SMTP_USE_TLS", True),
    )


st.set_page_config(page_title="Polymarket Alerts", layout="wide")
st.title("Polymarket Unusual Move Tracker")


if "monitor_state" not in st.session_state:
    st.session_state.monitor_state = MonitorState()
if "monitor" not in st.session_state:
    st.session_state.monitor = None
if "last_subscription" not in st.session_state:
    st.session_state.last_subscription = None

state: MonitorState = st.session_state.monitor_state

with st.sidebar:
    st.header("Settings")
    mode = st.selectbox("WebSocket mode", ["clob", "event"])
    ws_url = st.text_input(
        "WebSocket URL",
        value=os.getenv("POLYMARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com"),
    )
    top_n = st.slider("Top markets", 5, 50, int(os.getenv("TOP_MARKETS", "15")))
    min_volume = st.number_input(
        "Min volume ($)",
        min_value=0,
        value=int(os.getenv("MIN_VOLUME", "1000000")),
        step=100000,
    )
    threshold = st.slider("Alert threshold (probability)", 0.01, 0.5, 0.05, 0.01)
    window_minutes = st.slider("Window (minutes)", 1, 120, 10)
    cooldown_minutes = st.slider("Alert cooldown (minutes)", 1, 120, 5)
    price_source = st.selectbox("Price source", ["best_bid", "best_ask"])
    auto_refresh = st.checkbox("Auto refresh UI", value=True)
    refresh_interval = st.slider("UI refresh (seconds)", 1, 10, 2)
    event_filter = st.selectbox(
        "Event filter",
        ["All", "last_trade_price", "price_change", "book", "best_bid_ask"],
    )
    tags_data = []
    try:
        tags_data = fetch_tags()
    except Exception as exc:
        st.warning(f"Tag fetch failed: {exc}")
    tag_name_to_id = {tag["name"]: tag["id"] for tag in tags_data}
    selected_tags = st.multiselect(
        "Filter by tags",
        options=sorted(tag_name_to_id.keys()),
    )

    st.subheader("Email alerts")
    smtp_cfg = get_smtp_config()
    email_enabled = st.checkbox("Enable email alerts", value=smtp_cfg.enabled)
    if email_enabled and not smtp_cfg.ready:
        st.warning("SMTP config incomplete; email alerts will be skipped.")

    st.subheader("Advanced")
    subscription_override = st.text_area(
        "Subscription JSON override (optional)",
        value="",
        help="If provided, this JSON will be sent instead of the default subscription.",
    )

    start_clicked = st.button("Start tracking", type="primary")
    stop_clicked = st.button("Stop tracking")
    refresh_clicked = st.button("Refresh markets")

if refresh_clicked:
    fetch_top_markets.clear()
    fetch_tags.clear()
    fetch_events_by_tag.clear()

markets = []
try:
    markets = fetch_top_markets(top_n, min_volume)
except Exception as exc:
    st.error(f"Market discovery failed: {exc}")

if selected_tags:
    tagged_markets = []
    for tag_name in selected_tags:
        tag_id = tag_name_to_id.get(tag_name)
        if tag_id:
            tagged_markets.extend(fetch_events_by_tag(tag_id))
    # De-duplicate by market_id/condition_id
    seen = set()
    filtered = []
    for market in tagged_markets:
        key = market.get("market_id") or market.get("condition_id")
        if not key or key in seen:
            continue
        seen.add(key)
        filtered.append(market)
    markets = filtered

label_map = {}
for m in markets:
    if m.get("market_id"):
        label_map[m["market_id"]] = m["question"]
    if m.get("condition_id"):
        label_map[m["condition_id"]] = m["question"]
    if m.get("asset_id"):
        label_map[m["asset_id"]] = m["question"]
    if m.get("yes_asset_id"):
        label_map[m["yes_asset_id"]] = m["question"]
    if m.get("no_asset_id"):
        label_map[m["no_asset_id"]] = m["question"]

outcome_map = {}
for m in markets:
    yes_id = m.get("yes_asset_id")
    no_id = m.get("no_asset_id")
    if yes_id:
        outcome_map[yes_id] = "Yes"
    if no_id:
        outcome_map[no_id] = "No"

subscription_payload = None
if subscription_override.strip():
    try:
        subscription_payload = json.loads(subscription_override)
    except json.JSONDecodeError as exc:
        st.warning(f"Invalid subscription JSON: {exc}")
if subscription_payload is None:
    subscription_payload = build_subscription_payload(mode, markets)

effective_ws_url = normalize_ws_url(ws_url)
if "ws-subscriptions-clob.polymarket.com" in effective_ws_url and mode == "event":
    st.warning("Event mode isn't supported on the CLOB host; using market channel settings.")
    subscription_payload = build_subscription_payload("clob", markets)

if mode == "clob" and not subscription_payload.get("assets_ids"):
    st.warning("No valid asset IDs found for subscription. Check Gamma API response.")

with state.lock:
    state.config.update(
        {
            "threshold": threshold,
            "window_seconds": window_minutes * 60,
            "cooldown_seconds": cooldown_minutes * 60,
            "label_map": label_map,
            "outcome_map": outcome_map,
            "smtp_config": smtp_cfg,
            "email_enabled": email_enabled,
            "price_source": price_source,
            "event_filter": event_filter,
        }
    )

if start_clicked:
    st.session_state.monitor = MarketMonitor(state, effective_ws_url, subscription_payload)
    st.session_state.monitor.start()
    st.session_state.last_subscription = subscription_payload

if stop_clicked and st.session_state.monitor:
    st.session_state.monitor.stop()

status = state.status
st.subheader("Connection")
st.write("Connected:", bool(status.get("connected")))
st.write("Last message:", status.get("last_message_at") or "n/a")
if status.get("last_error"):
    st.error(status["last_error"])

table_col, alert_col = st.columns([2, 1])

with table_col:
    st.subheader("Markets")
    rows = []
    with state.lock:
        for m in markets:
            yes_asset_id = m.get("yes_asset_id")
            no_asset_id = m.get("no_asset_id")
            yes_price = state.prices.get(yes_asset_id)
            no_price = state.prices.get(no_asset_id)
            rows.append(
                {
                    "Market": m["question"],
                    "Yes": f"{yes_price:.2%}" if yes_price is not None else "n/a",
                    "No": f"{no_price:.2%}" if no_price is not None else "n/a",
                    "Volume24h": f"{m['volume24hr']:.0f}",
                }
            )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=400)

with alert_col:
    st.subheader("Alerts")
    with state.lock:
        alerts = list(state.alerts)
    if not alerts:
        st.write("No alerts yet.")
    else:
        for alert in alerts[:20]:
            st.warning(
                f"{alert['market']} moved {alert['delta']:.2%} "
                f"({alert['from']:.2%} → {alert['to']:.2%})"
            )

st.subheader("Live Event Stream")
with state.lock:
    events = list(state.events)
    active_filter = state.config.get("event_filter", "All")
if active_filter != "All":
    events = [event for event in events if event.get("event") == active_filter]
event_columns = [
    "time",
    "event",
    "market",
    "outcome",
    "best_bid",
    "best_ask",
    "side",
    "price",
    "size",
]
events_df = pd.DataFrame(events, columns=event_columns)
st.dataframe(events_df, use_container_width=True, height=300, key="events_table")

if auto_refresh:
    st_autorefresh(interval=refresh_interval * 1000, key="auto_refresh")
