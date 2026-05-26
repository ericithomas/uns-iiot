import json
import ssl
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import psycopg2

import os

BROKER = os.getenv("HIVEMQ_BROKER", "e172211a559b42289548a3dbeb0158ee.s1.eu.hivemq.cloud")
PORT = int(os.getenv("HIVEMQ_PORT", "8883"))
USERNAME = os.getenv("HIVEMQ_USER", "uns-edge-node")
PASSWORD = os.getenv("HIVEMQ_PASS")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "uns")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not PASSWORD:
    raise RuntimeError("Set HIVEMQ_PASS env var")
if not DB_PASSWORD:
    raise RuntimeError("Set DB_PASSWORD env var")


conn = psycopg2.connect(
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
)

conn.autocommit = True
cur = conn.cursor()


def insert_metric(device, metric, value):
    if isinstance(value, (int, float)):
        cur.execute(
            """
            INSERT INTO sensor_data (ts, device, metric, value)
            VALUES (%s, %s, %s, %s)
            """,
            (datetime.now(timezone.utc), device, metric, float(value)),
        )


def on_connect(client, userdata, flags, rc):
    print(f"Connected to HiveMQ, rc={rc}")
    client.subscribe("uns/json/#")
    print("Subscribed to uns/json/#")


def on_message(client, userdata, msg):
    try:
        device = msg.topic.split("/")[-1]
        payload = json.loads(msg.payload.decode("utf-8"))

        for metric, value in payload.items():
            insert_metric(device, metric, value)

        print(f"Inserted {device}: {payload}")

    except Exception as e:
        print(f"Error handling message on {msg.topic}: {e}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "historian-writer")
client.username_pw_set(USERNAME, PASSWORD)
client.tls_set(tls_version=ssl.PROTOCOL_TLS)

client.on_connect = on_connect
client.on_message = on_message

client.connect(BROKER, PORT, 60)
client.loop_forever()