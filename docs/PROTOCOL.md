# Zafro / I4season AC cloud protocol

Reverse‑engineered from a Zafro **A9092R‑12K** (Wi‑Fi module OUI `00:1C:C2`, Browan).
Captured by transparently MITM‑ing the device's own TLS on a network you control
(the device does not pin or validate the server certificate).

## Transport

- **Cloud host:** `zafro.nbrowan.com:443` (AWS), nginx front end.
- **TLS:** TLS 1.2 only, **RSA‑CBC ciphers only** (`AES256-SHA256`/`AES256-SHA`/
  `AES128-SHA256`/`AES128-SHA`), **no SNI** in the ClientHello, no cert validation.
- **Auth (REST):** `POST /iot1/device/login` with JSON
  `{"clientId","clientSecret","deviceSn","bizuserId"}` →
  `{"code":0,"data":{"access_token":...,"expires_in":604800,"token_type":"Bearer"}}`.
- **Time (REST):** `GET /iot1/time/second` → `{"code":0,"data":<unix seconds>}`.
- **Realtime:** `GET /ws/iot1/` upgrades to a WebSocket carrying **MQTT 3.1**
  ("MQIsdp", protocol level 3, 30 s keepalive). The AC requests `permessage-deflate`
  but **cannot read compressed server frames** — send WS frames uncompressed.

## MQTT topics

- Commands (cloud → device): `dev/<TENANT>/<SN>/command/request`
- State/replies (device → cloud): `dev/<TENANT>/<SN>/command/reply`
- Online/LWT (device → cloud): `lwt/<TENANT>/<SN>`

For Zafro, `<TENANT>` = `I4SEASON`. `<SN>` is the device serial (e.g. printed in the
app under WiFi Info). Both are learned automatically from the device's own published
topics, so no configuration is needed.

## Commands — `cmd:6` "set state"

Publish to `.../command/request`:
```json
{"cmd":6,"sn":null,"user":"<anything>","data":{"state":{ <field>:<value>, ... }}}
```

| Field | Meaning | Values |
|---|---|---|
| `poweron` | power | `true` / `false` |
| `mode` | operating mode | `1`=Cool, `2`=Dehumidify/Dry, `3`=Fan |
| `templevel` | target temperature | integer **°F** (direct, e.g. 70) |
| `rhlevel` | target humidity (dry mode) | integer %RH |
| `windlevel` | fan speed | `1`/`2`/`3` = speeds, `4` = Auto |
| `oscset1` | vertical swing | `true` / `false` |
| `oscset2` | horizontal swing | `true` / `false` |
| `eco` | eco mode | `true` / `false` |
| `extra` | turbo / boost | `true` / `false` |
| `sleep` | sleep mode | `true` / `false` |
| `lighton` | display light | `true` / `false` |
| `childlockon` | child lock | `true` / `false` |
| `muteon` | mute beeps | `true` / `false` |

Multiple fields may be combined in one `state` object. The app sends fan changes
bundled as `{"eco":…,"extra":…,"sleep":…,"windlevel":…}`.

## Reports — device → `.../command/reply`

- `cmd:5` — device info: `{"v":"I4SEASON","p":"<model>","ver":"<fw>","sn":...,"ssid":...,"rssi":...,"mcu_ver":...}`
- `cmd:4` — state report: a `result` object with any of the command fields above plus
  read‑only telemetry:

| Field | Meaning |
|---|---|
| `temperature` | current room temperature (°F) |
| `rh` | current relative humidity (%) |
| `waterlevel` | water tank full (0 = ok) |
| `reachtarget`, `worktime`, `filterthr` | misc telemetry |
| `tempunit` | 1 = °F |
| `timeron` / `timeroff` | `{ts,du}` schedule timers |
| `origin` | `0` = autonomous heartbeat (~every 12 s), `1` = response to a change |

## Notes

- The device holds one long‑lived MQTT‑over‑WS connection and only re‑resolves DNS
  when that connection drops — power‑cycle it to force it onto a redirected host.
- No credentials from any specific unit are included here; `clientId`/`clientSecret`
  are generated per device and sent by the device itself at login.
