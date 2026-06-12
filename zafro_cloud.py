#!/usr/bin/env python3
"""
Local cloud emulator + Home Assistant bridge for Zafro / I4season Wi-Fi air
conditioners (the "nbrowan.com" / I4SEASON IoT platform).

These ACs only talk to a vendor cloud (zafro.nbrowan.com on AWS). This program
pretends to BE that cloud on your own LAN: it answers the AC's REST login and
speaks the MQTT-over-WebSocket protocol the AC expects, then bridges the AC to
Home Assistant via MQTT Discovery. Result: full local control, no vendor cloud,
works with the internet unplugged.

The device serial number and tenant are learned automatically from the AC's own
traffic, so there is nothing device-specific to configure.

Config (all optional, via environment variables):
  MQTT_HOST              MQTT broker for Home Assistant   (default 127.0.0.1)
  MQTT_PORT                                               (default 1883)
  MQTT_USER / MQTT_PASS  broker auth, if you set any      (default none)
  LISTEN_PORT            plain-HTTP port nginx proxies to (default 8765)
  HA_DISCOVERY_PREFIX    HA MQTT discovery prefix         (default homeassistant)

See README.md for the full setup (DNS redirect, nginx TLS, systemd, HA).
License: MIT.
"""
import asyncio, json, time, struct, logging, os
from aiohttp import web, WSMsgType
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8765"))
HA_PREFIX = os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant")

NODE = "zafro_ac"
BASE = f"zafro/{NODE}"
DEVICE = {"identifiers": ["zafro_ac"], "name": "Zafro AC",
          "manufacturer": "Zafro / I4season", "model": "Wi-Fi AC"}

# Extra on/off controls exposed as HA switches: (topic suffix, AC field, name, icon)
SWITCHES = [
    ("light", "lighton", "Display Light", "mdi:lightbulb"),
    ("childlock", "childlockon", "Child Lock", "mdi:lock"),
    ("mute", "muteon", "Mute Beep", "mdi:volume-off"),
    ("eco", "eco", "Eco Mode", "mdi:leaf"),
    ("sleep", "sleep", "Sleep Mode", "mdi:power-sleep"),
    ("turbo", "extra", "Turbo", "mdi:fan-plus"),
    ("vswing", "oscset1", "Vertical Swing", "mdi:arrow-up-down"),
    ("hswing", "oscset2", "Horizontal Swing", "mdi:arrow-left-right"),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("zafro")

# ----------------------------------------------------------------------------
# Minimal MQTT 3.1/3.1.1 codec (only the packet types the AC uses)
# ----------------------------------------------------------------------------
def enc_len(n):
    out = b""
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out += bytes([b])
        if not n:
            break
    return out

def dec_len(buf, i):
    mult = 1
    val = 0
    while True:
        b = buf[i]
        i += 1
        val += (b & 0x7f) * mult
        if not (b & 0x80):
            break
        mult *= 128
    return val, i

def mk_publish(topic, payload, packet_id=None, qos=0):
    tb = topic.encode()
    body = struct.pack(">H", len(tb)) + tb
    if qos:
        body += struct.pack(">H", packet_id or 1)
    body += payload.encode() if isinstance(payload, str) else payload
    return bytes([0x30 | (qos << 1)]) + enc_len(len(body)) + body

def mk_connack():
    return bytes([0x20, 0x02, 0x00, 0x00])

def mk_suback(pid, n):
    return bytes([0x90]) + enc_len(2 + n) + struct.pack(">H", pid) + b"\x00" * n

def mk_puback(pid):
    return bytes([0x40, 0x02]) + struct.pack(">H", pid)

def mk_pingresp():
    return bytes([0xD0, 0x00])

# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------
LOOP = None
AC_OUT = None          # asyncio.Queue of bytes to send to the AC websocket
STATE = {}             # latest known AC state
DEV = {"req_topic": None, "sn": None}   # learned from the AC's own traffic
hac = None             # paho client to the Home Assistant MQTT broker

def ha_publish_state():
    if not hac:
        return
    power = STATE.get("poweron")
    mode_map = {1: "cool", 2: "dry", 3: "fan"}
    mode = "off" if power is False else mode_map.get(STATE.get("mode"), "cool")
    hac.publish(f"{BASE}/mode", mode, retain=True)
    if "templevel" in STATE:
        hac.publish(f"{BASE}/temp", STATE["templevel"], retain=True)
    if "rhlevel" in STATE:
        hac.publish(f"{BASE}/targethum", STATE["rhlevel"], retain=True)
    fan_map = {1: "1", 2: "2", 3: "3", 4: "auto"}
    if "windlevel" in STATE:
        hac.publish(f"{BASE}/fan", fan_map.get(STATE["windlevel"], "auto"), retain=True)
    if "rh" in STATE:
        hac.publish(f"{BASE}/humidity", STATE["rh"], retain=True)
    if "temperature" in STATE:
        hac.publish(f"{BASE}/curtemp", STATE["temperature"], retain=True)
    for suffix, field, _name, _icon in SWITCHES:
        if field in STATE:
            hac.publish(f"{BASE}/{suffix}", "on" if STATE[field] else "off", retain=True)
    if "waterlevel" in STATE:
        hac.publish(f"{BASE}/watertank", "on" if STATE["waterlevel"] else "off", retain=True)

def send_to_ac(state_fields):
    if not DEV["req_topic"]:
        log.warning("AC not identified yet; dropping command %s", state_fields)
        return
    payload = json.dumps({"cmd": 6, "sn": None, "user": "ha_bridge", "data": {"state": state_fields}})
    pkt = mk_publish(DEV["req_topic"], payload)
    if LOOP and AC_OUT:
        LOOP.call_soon_threadsafe(AC_OUT.put_nowait, pkt)
        log.info("-> AC %s", state_fields)

# ----------------------------------------------------------------------------
# Home Assistant (paho) side: MQTT Discovery + command handling
# ----------------------------------------------------------------------------
def on_ha_connect(c, u, f, rc, props=None):
    log.info("HA MQTT connected")
    c.publish(f"{HA_PREFIX}/climate/{NODE}/config", "", retain=True)  # remove any legacy climate entity
    c.publish(f"{HA_PREFIX}/select/{NODE}_mode/config", json.dumps({
        "name": "Mode", "unique_id": "zafro_mode",
        "options": ["off", "cool", "dry", "fan"],
        "command_topic": f"{BASE}/mode/set", "state_topic": f"{BASE}/mode",
        "icon": "mdi:air-conditioner", "device": DEVICE}), retain=True)
    c.publish(f"{HA_PREFIX}/number/{NODE}_temp/config", json.dumps({
        "name": "Target Temperature", "unique_id": "zafro_temp",
        "command_topic": f"{BASE}/temp/set", "state_topic": f"{BASE}/temp",
        "min": 60, "max": 86, "step": 1, "unit_of_measurement": "°F",
        "mode": "box", "icon": "mdi:thermometer", "device": DEVICE}), retain=True)
    c.publish(f"{HA_PREFIX}/number/{NODE}_targethum/config", json.dumps({
        "name": "Target Humidity", "unique_id": "zafro_targethum",
        "command_topic": f"{BASE}/targethum/set", "state_topic": f"{BASE}/targethum",
        "min": 30, "max": 80, "step": 1, "unit_of_measurement": "%",
        "mode": "box", "icon": "mdi:water-percent", "device": DEVICE}), retain=True)
    c.publish(f"{HA_PREFIX}/select/{NODE}_fan/config", json.dumps({
        "name": "Fan Speed", "unique_id": "zafro_fan",
        "options": ["auto", "1", "2", "3"],
        "command_topic": f"{BASE}/fan/set", "state_topic": f"{BASE}/fan",
        "icon": "mdi:fan", "device": DEVICE}), retain=True)
    c.publish(f"{HA_PREFIX}/sensor/{NODE}_curtemp/config", json.dumps({
        "name": "Current Temperature", "unique_id": "zafro_curtemp",
        "state_topic": f"{BASE}/curtemp", "unit_of_measurement": "°F",
        "device_class": "temperature", "device": DEVICE}), retain=True)
    c.publish(f"{HA_PREFIX}/sensor/{NODE}_curhum/config", json.dumps({
        "name": "Current Humidity", "unique_id": "zafro_curhum",
        "state_topic": f"{BASE}/humidity", "unit_of_measurement": "%",
        "device_class": "humidity", "device": DEVICE}), retain=True)
    for t in ("mode/set", "temp/set", "fan/set", "targethum/set"):
        c.subscribe(f"{BASE}/{t}")
    for suffix, field, name, icon in SWITCHES:
        c.publish(f"{HA_PREFIX}/switch/{NODE}_{suffix}/config", json.dumps({
            "name": name, "unique_id": f"zafro_{suffix}",
            "command_topic": f"{BASE}/{suffix}/set", "state_topic": f"{BASE}/{suffix}",
            "payload_on": "on", "payload_off": "off", "icon": icon, "device": DEVICE}), retain=True)
        c.subscribe(f"{BASE}/{suffix}/set")
    c.publish(f"{HA_PREFIX}/binary_sensor/{NODE}_watertank/config", json.dumps({
        "name": "Water Tank Full", "unique_id": "zafro_watertank",
        "state_topic": f"{BASE}/watertank", "payload_on": "on", "payload_off": "off",
        "device_class": "problem", "icon": "mdi:cup-water", "device": DEVICE}), retain=True)

def on_ha_msg(c, u, m):
    t = m.topic
    v = m.payload.decode()
    log.info("HA cmd %s = %s", t, v)
    if t.endswith("/mode/set"):
        if v == "off":
            send_to_ac({"poweron": False})
        else:
            send_to_ac({"poweron": True})
            send_to_ac({"mode": {"cool": 1, "dry": 2, "fan": 3}.get(v, 1)})
    elif t.endswith("/temp/set"):
        send_to_ac({"templevel": int(float(v))})
    elif t.endswith("/fan/set"):
        send_to_ac({"windlevel": {"auto": 4, "1": 1, "2": 2, "3": 3}.get(v, 4)})
    elif t.endswith("/targethum/set"):
        send_to_ac({"rhlevel": int(float(v))})
    else:
        for suffix, field, _name, _icon in SWITCHES:
            if t.endswith(f"/{suffix}/set"):
                send_to_ac({field: v == "on"})
                break

# ----------------------------------------------------------------------------
# REST endpoints the AC calls before opening its WebSocket
# ----------------------------------------------------------------------------
async def login(req):
    body = await req.text()
    log.info("AC login: %s", body)
    return web.json_response({"code": 0, "msg": "ok", "data": {
        "access_token": "localtoken", "expires_in": 604800,
        "scope": "all", "token_type": "Bearer"}})

async def time_second(req):
    return web.json_response({"code": 0, "msg": "ok", "data": int(time.time())})

# ----------------------------------------------------------------------------
# WebSocket = the AC's MQTT transport. We act as a tiny MQTT broker for it.
# ----------------------------------------------------------------------------
async def ws_iot(req):
    global AC_OUT
    # compress=False is REQUIRED: the AC requests permessage-deflate but cannot
    # read compressed server frames (it silently dies at the keepalive otherwise).
    ws = web.WebSocketResponse(protocols=("mqtt",), compress=False)
    await ws.prepare(req)
    log.info("AC websocket connected")
    AC_OUT = asyncio.Queue()
    buf = bytearray()

    async def sender():
        while not ws.closed:
            pkt = await AC_OUT.get()
            await ws.send_bytes(pkt)

    send_task = asyncio.create_task(sender())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.BINARY:
                continue
            buf.extend(msg.data)
            while buf:
                if len(buf) < 2:
                    break
                ptype = buf[0] & 0xF0
                flags0 = buf[0]
                rl, hdr = dec_len(buf, 1)
                if len(buf) < hdr + rl:
                    break
                pkt = bytes(buf[:hdr + rl])
                body = pkt[hdr:]
                del buf[:hdr + rl]
                if ptype == 0x10:                                  # CONNECT
                    await ws.send_bytes(mk_connack())
                elif ptype == 0x80:                                # SUBSCRIBE
                    pid = struct.unpack(">H", body[:2])[0]
                    i, n = 2, 0
                    while i < len(body):
                        tl = struct.unpack(">H", body[i:i+2])[0]
                        i += 2 + tl + 1
                        n += 1
                    await ws.send_bytes(mk_suback(pid, n))
                elif ptype == 0x30:                                # PUBLISH (state from AC)
                    qos = (flags0 >> 1) & 3
                    tl = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2+tl].decode(errors="replace")
                    off = 2 + tl
                    pid = None
                    if qos:
                        pid = struct.unpack(">H", body[off:off+2])[0]
                        off += 2
                    payload = body[off:]
                    # Learn the device's tenant + SN from its own topics (dev/<tenant>/<sn>/...)
                    if DEV["sn"] is None and "/" in topic:
                        parts = topic.split("/")
                        if len(parts) >= 3 and parts[0] in ("dev", "lwt"):
                            DEV["sn"] = parts[2]
                            DEV["req_topic"] = f"dev/{parts[1]}/{parts[2]}/command/request"
                            log.info("identified AC: tenant=%s sn=%s", parts[1], parts[2])
                    try:
                        data = json.loads(payload.decode(errors="replace"))
                        res = data.get("result") or (data.get("data") or {}).get("state") or {}
                        if isinstance(res, dict):
                            STATE.update({k: v for k, v in res.items() if k != "origin"})
                            ha_publish_state()
                    except Exception:
                        pass
                    if qos:
                        await ws.send_bytes(mk_puback(pid))
                elif ptype == 0xC0:                                # PINGREQ
                    await ws.send_bytes(mk_pingresp())
    finally:
        send_task.cancel()
        AC_OUT = None
        log.info("AC websocket closed")
    return ws

def main():
    global LOOP, hac
    LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(LOOP)
    hac = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
    if MQTT_USER:
        hac.username_pw_set(MQTT_USER, MQTT_PASS)
    hac.on_connect = on_ha_connect
    hac.on_message = on_ha_msg
    hac.connect(MQTT_HOST, MQTT_PORT, 60)
    hac.loop_start()
    app = web.Application()
    app.add_routes([
        web.post("/iot1/device/login", login),
        web.get("/iot1/time/second", time_second),
        web.get("/ws/iot1/", ws_iot),
    ])
    log.info("zafro-cloud listening on 127.0.0.1:%d (HA broker %s:%d)", LISTEN_PORT, MQTT_HOST, MQTT_PORT)
    web.run_app(app, host="127.0.0.1", port=LISTEN_PORT, loop=LOOP, print=None)

if __name__ == "__main__":
    main()
