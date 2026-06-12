# Zafro / I4season AC — local Home Assistant control (no cloud)

Control a **Zafro** (or other **I4season / nbrowan.com platform**) Wi‑Fi air conditioner
entirely from **Home Assistant on your own LAN**, with the vendor cloud cut out
completely. Works with the internet unplugged.

These ACs (e.g. the Zafro **A9092R**) ship with the "Zafro Smart" app and only ever
talk to a vendor cloud at `zafro.nbrowan.com` (hosted on AWS). There is no local API,
no Tuya/ESPHome support, and nothing on the network you can talk to directly. This
project makes a small server on your LAN **pretend to be that cloud**: it answers the
AC's login, speaks the same MQTT‑over‑WebSocket protocol the AC expects, and bridges
the AC into Home Assistant via MQTT Discovery.

The result is a full set of native HA entities (mode, temperature, humidity, fan,
swings, eco/sleep/turbo, display light, child lock, water‑tank sensor, live
temperature & humidity) that drive the real unit — **with no data leaving your house.**

> ⚠️ **Scope / safety.** This talks only to *your own* AC, on *your own* network.
> It does not attack the vendor, and it relies on the fact that these devices do not
> validate the TLS certificate they connect to. It's a personal‑interoperability tool.
> Tested against a Zafro A9092R‑12K (firmware 1.0.28); other I4season‑platform models
> very likely use the same protocol but YMMV.

---

## How it works

```
  AC  ──TLS──►  nginx (your server, :443)  ──►  zafro_cloud.py  ──MQTT──►  Mosquitto  ──►  Home Assistant
       (thinks it's              (terminates TLS,        (fake cloud +              (broker)
        zafro.nbrowan.com)        proxies REST + WS)      HA bridge)
```

1. You point the AC's cloud hostname (`zafro.nbrowan.com`) at your server via DNS.
2. **nginx** terminates the AC's TLS with a self‑signed cert and proxies to `zafro_cloud.py`.
3. **`zafro_cloud.py`** answers the REST login and acts as a tiny MQTT broker over the
   AC's WebSocket, then mirrors the AC to/from Home Assistant through **Mosquitto** using
   MQTT Discovery — so the entities appear automatically.

The AC's serial number is learned automatically from its own traffic — nothing
device‑specific to configure.

---

## Requirements

- A small always‑on Linux box on the same LAN (a Pi, an old PC, your HA host, etc.)
- **Home Assistant** with the **MQTT integration**
- **Mosquitto** (or any MQTT broker) — can be the HA add‑on or a system package
- **nginx**
- **Python 3.9+**
- A way to override DNS for the AC's hostname on your network (see step 5) — e.g. a
  router that supports DNS host mapping, or Pi‑hole, or dnsmasq.

---

## Setup

### 1. Install the bridge

```bash
sudo mkdir -p /opt/zafro-cloud
sudo chown $USER /opt/zafro-cloud
python3 -m venv /opt/zafro-cloud/venv
/opt/zafro-cloud/venv/bin/pip install -r requirements.txt   # paho-mqtt aiohttp
cp zafro_cloud.py /opt/zafro-cloud/
```

### 2. Mosquitto (if you don't already have a broker)

```bash
sudo apt install mosquitto mosquitto-clients
sudo cp deploy/mosquitto-local.conf /etc/mosquitto/conf.d/local.conf
sudo systemctl restart mosquitto
```
Then add the **MQTT integration** in Home Assistant pointed at this broker
(`127.0.0.1:1883` if it's on the HA host). If your broker needs auth, set
`MQTT_USER` / `MQTT_PASS` in the systemd unit (step 4).

### 3. Self‑signed certificate

The AC does **not** validate the certificate, so a self‑signed one is fine:

```bash
sudo bash deploy/gen-cert.sh        # writes /etc/zafro-cloud/{cert,key}.pem
```

### 4. nginx vhost

```bash
sudo cp deploy/nginx-zafro.conf /etc/nginx/sites-available/zafro.conf
sudo ln -s /etc/nginx/sites-available/zafro.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```
Two things in that config are **essential** (see [Gotchas](#gotchas)):
- `default_server` — the AC sends **no SNI**, so this vhost must be the default.
- the legacy `ssl_ciphers` line — the AC only offers old TLS 1.2 RSA ciphers.

### 5. Run the bridge as a service

```bash
sudo cp deploy/zafro-cloud.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zafro-cloud
journalctl -u zafro-cloud -f      # watch it
```

### 6. Redirect the AC's DNS to your server

Make `zafro.nbrowan.com` resolve to your server's IP **for the AC**. Pick whichever
fits your network:

- **Router with "DNS Host Mapping" / static DNS hosts** (many ISP routers, e.g. Calix):
  add `zafro.nbrowan.com → <your-server-ip>`. Cleanest — only the AC ever looks this
  name up, so nothing else is affected.
- **Pi‑hole / AdGuard / dnsmasq:** add a local DNS record
  `zafro.nbrowan.com → <your-server-ip>` and make sure the AC uses it as its resolver.
  (dnsmasq: `address=/zafro.nbrowan.com/<your-server-ip>`.)

Verify from another machine: `dig +short zafro.nbrowan.com` → your server's IP.

### 7. Power‑cycle the AC

The AC caches its cloud connection, so unplug it for ~10 s and plug it back in. On
boot it does a fresh DNS lookup, lands on your server, and within a minute the entities
in Home Assistant come alive. Watch `journalctl -u zafro-cloud -f` — you should see
`AC websocket connected`, `identified AC: ...`, and state publishes.

---

## Entities created

All grouped under one **Zafro AC** device in Home Assistant:

| Entity | Type | Notes |
|---|---|---|
| Mode | select | off / cool / dry / fan |
| Target Temperature | number | °F, used in cool mode |
| Target Humidity | number | %, used in dry mode |
| Fan Speed | select | auto / 1 / 2 / 3 |
| Current Temperature | sensor | live room temp |
| Current Humidity | sensor | live humidity |
| Display Light, Child Lock, Mute Beep, Eco, Sleep, Turbo, Vertical Swing, Horizontal Swing | switch | |
| Water Tank Full | binary_sensor | `problem` class |

---

## Gotchas

These cost real debugging time; they're baked into the provided configs:

- **No SNI.** The AC's TLS ClientHello sends no server name, so nginx must serve the
  zafro vhost as `listen 443 ssl default_server;` or it falls through to another vhost
  with strict ciphers and the handshake fails (a 7‑byte TLS alert).
- **Ancient ciphers.** The AC offers only TLS 1.2 RSA‑CBC suites
  (`AES256-SHA256`, `AES256-SHA`, …). Modern OpenSSL disables these, so the nginx vhost
  pins `ssl_ciphers "AES256-SHA256:AES128-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=0"; ssl_protocols TLSv1.2;`.
- **MQTT 3.1 + no WebSocket compression.** The AC speaks MQTT **3.1** ("MQIsdp", level 3,
  30 s keepalive) and requests `permessage-deflate` but cannot read compressed server
  frames — it CONNECTs then silently dies at the 30 s keepalive. The bridge therefore
  uses `WebSocketResponse(compress=False)`.

---

## Protocol

Full reverse‑engineered protocol and the complete field map are in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## Finding your device's cloud hostname (other brands)

This targets `zafro.nbrowan.com`. Other I4season rebrands may use a different
`<brand>.nbrowan.com` host. To find yours, watch the AC's DNS queries (e.g. in your
router/Pi‑hole query log, or `tcpdump -ni <iface> 'host <ac-ip> and port 53'`) and look
for an `*.nbrowan.com` lookup. Use that hostname everywhere this README says
`zafro.nbrowan.com` (the cert CN and nginx `server_name` can stay generic since the AC
ignores them, but the DNS override must match the real hostname).

---

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Zafro, I4season, or Browan. "Zafro" and other names are trademarks
of their respective owners. Use at your own risk on hardware you own.
