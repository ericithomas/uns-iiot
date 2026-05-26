import json
import ssl
import time
from datetime import datetime, timezone

import gspread
import paho.mqtt.client as mqtt
from google.oauth2.service_account import Credentials

import os

BROKER = os.getenv("HIVEMQ_BROKER", "e172211a559b42289548a3dbeb0158ee.s1.eu.hivemq.cloud")
PORT = int(os.getenv("HIVEMQ_PORT", "8883"))
USERNAME = os.getenv("HIVEMQ_USER", "uns-edge-node")
PASSWORD = os.getenv("HIVEMQ_PASS")

SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "C:/Users/Eric/uns-project/credentials/google-sheets-key.json"
)
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "UNS Maintenance Orders")

COOLDOWN_SECONDS = int(os.getenv("ERP_COOLDOWN_SECONDS", "300"))

if not PASSWORD:
    raise RuntimeError("Set HIVEMQ_PASS env var")

last_work_order_time = 0


creds = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=SCOPES,
)

gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1


def append_work_order(score, source="MQTT"):
    global last_work_order_time

    now = time.time()
    if now - last_work_order_time < COOLDOWN_SECONDS:
        print(f"Skipped duplicate alarm due to cooldown. Score={score}")
        return

    last_work_order_time = now

    row = [
        datetime.now(timezone.utc).isoformat(),
        "Motor1",
        "AnomalyScore",
        score,
        "OPEN",
        f"Auto-created from edge anomaly via {source}",
    ]

    sheet.append_row(row)
    print(f"Created work order: score={score}")


def on_connect(client, userdata, flags, rc):
    print(f"Connected to HiveMQ, rc={rc}")
    client.subscribe("uns/anomaly")
    print("Subscribed to uns/anomaly")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        score = payload.get("AnomalyScore")

        if score is None:
            print(f"Ignored message without AnomalyScore: {payload}")
            return

        append_work_order(score, source="uns/anomaly")

    except Exception as e:
        print(f"Error handling message on {msg.topic}: {e}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "erp-handler")
client.username_pw_set(USERNAME, PASSWORD)
client.tls_set(tls_version=ssl.PROTOCOL_TLS)

client.on_connect = on_connect
client.on_message = on_message

client.connect(BROKER, PORT, 60)
client.loop_forever()