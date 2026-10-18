"""End-to-end protocol check against a tiny local MQTT acknowledgement server."""

import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

import app
from mqtt_client import _read_packet


class FakeBroker:
    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.messages = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            conn, _ = self.server.accept()
            with conn:
                conn.settimeout(10)
                packet, _ = _read_packet(conn)
                assert packet == 0x10
                conn.sendall(b"\x20\x02\x00\x00")
                while True:
                    packet, body = _read_packet(conn)
                    if packet == 0xE0:
                        break
                    assert packet == 0x32
                    topic_len = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + topic_len].decode()
                    packet_id = body[2 + topic_len:4 + topic_len]
                    payload = json.loads(body[4 + topic_len:])
                    self.messages.append((topic, payload))
                    conn.sendall(b"\x40\x02" + packet_id)
        finally:
            self.server.close()


class MQTTIntegrationTest(unittest.TestCase):
    def test_full_catalog_publishes_acknowledged_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            original = app.DB_PATH
            app.DB_PATH = Path(directory) / "test.sqlite3"
            try:
                app.init_db()
                broker = FakeBroker()
                broker.thread.start()
                with app.db() as conn:
                    conn.execute("UPDATE settings SET value='127.0.0.1' WHERE key='host'")
                    conn.execute("UPDATE settings SET value=? WHERE key='port'", (str(broker.port),))
                    conn.execute("UPDATE sensors SET baseline=50,current_value=600 WHERE id='raw-material-warehouse--electricity'")
                app.publish_batch()
                broker.thread.join(10)
                self.assertFalse(broker.thread.is_alive())
                self.assertEqual(len(broker.messages), 66)
                self.assertEqual(app.public_status()["state"], "connected")
                self.assertTrue(all(message["simulated"] for _, message in broker.messages))
                self.assertTrue(all("baseline" not in message for _, message in broker.messages))
                self.assertTrue(all(topic.startswith("factory/wheel-rim/") for topic, _ in broker.messages))
                electricity = next(message for _, message in broker.messages
                                   if message["sensor_id"] == "raw-material-warehouse--electricity")
                self.assertEqual(electricity["value"], 600)
                with app.db() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 66)
                    self.assertEqual(conn.execute("SELECT baseline FROM sensors WHERE id='raw-material-warehouse--electricity'").fetchone()[0], 50)
            finally:
                app.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
