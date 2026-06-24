import json
import ssl
from datetime import datetime, timezone

import os
import paho.mqtt.client as mqtt
import psycopg2


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


METER_INFO = {
    "Incomer": {
        "meter_id": "Incomer",
        "meter_path": "Energy/Incomer",
        "unit_id": 1,
    },
    "PackLine1": {
        "meter_id": "PackLine1",
        "meter_path": "Energy/DB1/PackLine1",
        "unit_id": 2,
    },
    "Utilities": {
        "meter_id": "Utilities",
        "meter_path": "Energy/DB1/Utilities",
        "unit_id": 3,
    },
}


INSERT_COLUMNS = [
    "ts",
    "meter_id",
    "meter_path",
    "unit_id",

    "v_l1",
    "v_l2",
    "v_l3",

    "i_l1",
    "i_l2",
    "i_l3",

    "p_kw",
    "q_kvar",
    "s_kva",
    "pf",
    "freq_hz",

    "thd_v_pct",
    "thd_i_pct",

    "p_l1_kw",
    "p_l2_kw",
    "p_l3_kw",

    "pf_l1",
    "pf_l2",
    "pf_l3",

    "kwh",
    "kvarh",
    "demand_kw",

    "apfc_pf_before",
    "apfc_pf_after",
    "apfc_q_cap_kvar",
    "apfc_steps_active",
    "apfc_target_pf",
    "apfc_q_before_kvar",
    "apfc_q_after_kvar",

    "source",
]


conn = psycopg2.connect(
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
)

conn.autocommit = True
cur = conn.cursor()


def clean_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def insert_energy_reading(meter_name, payload):
    if meter_name not in METER_INFO:
        print(f"Skipping unknown meter: {meter_name}")
        return

    info = METER_INFO[meter_name]

    values = [
        datetime.now(timezone.utc),
        info["meter_id"],
        info["meter_path"],
        info["unit_id"],

        clean_float(payload.get("v_l1")),
        clean_float(payload.get("v_l2")),
        clean_float(payload.get("v_l3")),

        clean_float(payload.get("i_l1")),
        clean_float(payload.get("i_l2")),
        clean_float(payload.get("i_l3")),

        clean_float(payload.get("p_kw")),
        clean_float(payload.get("q_kvar")),
        clean_float(payload.get("s_kva")),
        clean_float(payload.get("pf")),

        clean_float(payload.get("freq_hz")),

        clean_float(payload.get("thd_v_pct")),
        clean_float(payload.get("thd_i_pct")),

        clean_float(payload.get("p_l1_kw")),
        clean_float(payload.get("p_l2_kw")),
        clean_float(payload.get("p_l3_kw")),

        clean_float(payload.get("pf_l1")),
        clean_float(payload.get("pf_l2")),
        clean_float(payload.get("pf_l3")),

        clean_float(payload.get("kwh")),
        clean_float(payload.get("kvarh")),
        clean_float(payload.get("demand_kw")),

        clean_float(payload.get("apfc_pf_before")),
        clean_float(payload.get("apfc_pf_after")),
        clean_float(payload.get("apfc_q_cap_kvar")),
        clean_float(payload.get("apfc_steps_active")),
        clean_float(payload.get("apfc_target_pf")),
        clean_float(payload.get("apfc_q_before_kvar")),
        clean_float(payload.get("apfc_q_after_kvar")),

        "sentron_emulator",
    ]

    columns_sql = ", ".join(INSERT_COLUMNS)
    placeholders_sql = ", ".join(["%s"] * len(INSERT_COLUMNS))

    cur.execute(
        f"""
        INSERT INTO energy.meter_readings_raw (
            {columns_sql}
        )
        VALUES (
            {placeholders_sql}
        )
        """,
        values,
    )


def on_connect(client, userdata, flags, rc):
    print(f"Connected to HiveMQ, rc={rc}")
    client.subscribe("uns/json/energy/#")
    print("Subscribed to uns/json/energy/#")


def on_message(client, userdata, msg):
    try:
        meter_name = msg.topic.split("/")[-1]
        raw = msg.payload.decode("utf-8", errors="replace").strip()

        if not raw:
            print(f"Empty payload on {msg.topic}, skipping")
            return

        payload = json.loads(raw)

        insert_energy_reading(meter_name, payload)

        if meter_name == "Incomer":
            print(
                f"Inserted {meter_name}: "
                f"P={payload.get('p_kw')} kW, "
                f"PF={payload.get('pf')}, "
                f"APFC before={payload.get('apfc_pf_before')}, "
                f"after={payload.get('apfc_pf_after')}, "
                f"steps={payload.get('apfc_steps_active')}, "
                f"kVAR={payload.get('apfc_q_cap_kvar')}"
            )
        else:
            print(
                f"Inserted {meter_name}: "
                f"P={payload.get('p_kw')} kW, "
                f"PF={payload.get('pf')}, "
                f"THD_I={payload.get('thd_i_pct')}"
            )

    except Exception as e:
        print(f"Error handling message on {msg.topic}: {e}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "energy-historian-writer")
client.username_pw_set(USERNAME, PASSWORD)
client.tls_set(tls_version=ssl.PROTOCOL_TLS)

client.on_connect = on_connect
client.on_message = on_message

print(f"Connecting to TimescaleDB database: {DB_NAME}")
print("Connected to TimescaleDB.")

client.connect(BROKER, PORT, 60)
client.loop_forever()