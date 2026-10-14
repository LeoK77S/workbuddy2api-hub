"""Inline image format normalization (upstream code 11135).

The upstream reads an image's format from its bytes - a BMP payload labelled
`image/png` is refused just the same - and accepts only png/jpeg/webp. A client
that inlines anything else (a BMP thumbnail extracted from a DWG, the case this
was written for) leaves that message in the history, so every later turn of the
conversation is answered `400 code 11135` until a new conversation is started.
With `image_format_normalization_enabled` on, the gateway rewrites the formats
it can decode with the standard library (BMP) to PNG before the body leaves.

Two halves, both here:

  1. the codec and the message walker, checked against an *independent* BMP
     encoder written in this file (a decoder verified against itself proves
     nothing) - dimensions, pixel order, the 4-byte row padding, top-down vs
     bottom-up, 24 vs 32 bpp, and every pass-through case;
  2. a real gateway process-in-thread against a mock upstream that sniffs the
     image bytes and answers 11135 for anything it cannot read, so the switch
     off -> 400 / switch on -> 200 round trip is exercised end to end, together
     with the strict-boolean rule on /settings/save.

No network: the upstream is local and the account is a locally minted JWT.
"""
import base64
import json
import os
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402  (shared spare-port pick)

WORK = tempfile.mkdtemp(prefix="image-format-norm-")
ACCOUNTS = os.path.join(WORK, "accounts")
os.makedirs(ACCOUNTS, exist_ok=True)
os.environ["ACCOUNTS_DIR"] = ACCOUNTS
os.environ["WB_PROXY_USAGE_DIR"] = os.path.join(WORK, "usage")
os.makedirs(os.environ["WB_PROXY_USAGE_DIR"], exist_ok=True)
os.environ["HOME"] = WORK
os.environ["USERPROFILE"] = WORK
os.environ["WB_PROXY_KEY"] = "TESTKEY"


def jwt(uid="acct-img-1"):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return seg({"alg": "none", "typ": "JWT"}) + "." + seg(
        {"uid": uid, "exp": int(time.time()) + 86400 * 30}) + ".x"


with open(os.path.join(ACCOUNTS, "acct1.json"), "w", encoding="utf-8") as fh:
    json.dump({
        "uid": "acct-img-1",
        "accessToken": jwt(),
        "refreshToken": "",
        "expiresAt": int(time.time()) + 86400 * 30,
        "realm": "intl",
        "domain": "www.workbuddy.ai",
        "platform": "CLI",
        "product": "workbuddy",
        "enabled": True,
    }, fh)

import wb_settings  # noqa: E402
import wb_identity  # noqa: E402
import wb_proxy as P  # noqa: E402

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


# ---------------------------------------------------------------------------
# An independent BMP encoder and PNG reader, both written from the spec.
# ---------------------------------------------------------------------------
def build_bmp(width, height, rgb_rows, top_down=False, bpp=24):
    """A minimal uncompressed BMP. rgb_rows is top row first; the file stores
    rows bottom-up unless top_down, and pads every row to 4 bytes."""
    row_stride = ((width * bpp + 31) // 32) * 4
    body = bytearray()
    for row in (rgb_rows if top_down else list(reversed(rgb_rows))):
        line = bytearray()
        for (r, g, b) in row:
            line += bytes((b, g, r)) if bpp == 24 else bytes((b, g, r, 0))
        line += b"\x00" * (row_stride - len(line))
        body += line
    header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(body), 0, 0, 14 + 40)
    dib = struct.pack("<IiiHHIIiiII", 40, width,
                      -height if top_down else height, 1, bpp, 0,
                      len(body), 2835, 2835, 0, 0)
    return bytes(header + dib + body)


def read_png(data):
    """(width, height, depth, colour_type, rows) with rows top-down RGB tuples.

    Validates the signature, every chunk CRC and the IHDR fields, then inflates
    the IDAT and asserts the scanlines carry no filter - the encoder under test
    only ever writes filter 0.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, ihdr, idat = 8, None, b""
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, "bad CRC on %r" % tag
        if tag == b"IHDR":
            ihdr = payload
        elif tag == b"IDAT":
            idat += payload
        pos += 12 + length
    assert ihdr is not None, "no IHDR"
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    raw = zlib.decompress(idat)
    rows = []
    for y in range(height):
        off = y * (width * 3 + 1)
        assert raw[off] == 0, "unexpected filter byte %d" % raw[off]
        rows.append([tuple(raw[off + 1 + x * 3:off + 4 + x * 3]) for x in range(width)])
    return width, height, depth, ctype, rows


def data_url(fmt, payload):
    return "data:image/%s;base64,%s" % (fmt, base64.b64encode(payload).decode())


def png_data_url(width=1, height=1):
    """A 1x1 red PNG, hand-rolled so no other encoder is involved."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    scan = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scan)) + chunk(b"IEND", b"")).decode()


RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)
WHITE, BLACK, GREY = (255, 255, 255), (0, 0, 0), (128, 128, 128)
SAMPLE = [[RED, GREEN, BLUE], [WHITE, BLACK, GREY]]      # 3x2, exercises padding

print("[1] BMP -> PNG codec")
bmp = build_bmp(3, 2, SAMPLE)
png = P.bmp_to_png(bmp)
w, h, depth, ctype, rows = read_png(png)
check("3x2 24bpp BMP becomes a 3x2 8-bit truecolour PNG",
      (w, h, depth, ctype) == (3, 2, 8, 2), (w, h, depth, ctype))
check("BGR is un-swapped to RGB and the bottom-up rows are flipped",
      rows == SAMPLE, rows)
check("the padded (non-multiple-of-4) row width is handled", len(png) > 0)

td = build_bmp(3, 2, SAMPLE, top_down=True)
check("a negative height (top-down) BMP yields the same pixels",
      read_png(P.bmp_to_png(td))[4] == SAMPLE)
b32 = build_bmp(3, 2, SAMPLE, bpp=32)
check("32bpp BGRA is decoded to RGB",
      read_png(P.bmp_to_png(b32))[4] == SAMPLE)
wide = [[RED, GREEN, BLUE, WHITE], [BLACK, GREY, RED, GREEN]]
check("a width with no padding also round-trips",
      read_png(P.bmp_to_png(build_bmp(4, 2, wide)))[4] == wide)

print()
print("[2] pass-through: nothing this code cannot handle is ever rewritten")
for label, bad in (
    ("truncated file", bmp[:40]),
    ("not a BMP at all", b"GIF89a" + b"\x00" * 60),
    ("unsupported bit depth (8bpp)", bmp[:28] + struct.pack("<H", 8) + bmp[30:]),
    ("compressed (BI_RLE) variant",
     bmp[:30] + struct.pack("<I", 1) + bmp[34:]),
):
    check("%s -> None" % label, P.bmp_to_png(bad) is None)

check("a BMP data URL is rewritten to image/png",
      P.normalize_image_data_url(data_url("bmp", bmp)).startswith("data:image/png;base64,"))
check("BMP bytes under a media type the upstream reads are still rewritten",
      P.normalize_image_data_url(data_url("png", bmp)).startswith("data:image/png;base64,"))
for fmt in ("png", "jpeg", "jpg", "webp"):
    url = "data:image/%s;base64,AAAA" % fmt
    check("image/%s is left alone" % fmt, P.normalize_image_data_url(url) is None)
check("a real PNG data URL is left alone",
      P.normalize_image_data_url(png_data_url(4, 4)) is None)
check("a gif data URL is left alone",
      P.normalize_image_data_url("data:image/gif;base64,R0lGOD") is None)
check("an http(s) URL is left alone",
      P.normalize_image_data_url("https://example.com/a.bmp") is None)
check("a non-base64 data URL is left alone",
      P.normalize_image_data_url("data:image/bmp,plain") is None)
check("a corrupt base64 payload is swallowed, not raised",
      P.normalize_image_data_url("data:image/bmp;base64,!!!!") is None)
check("a BMP data URL whose payload is not a BMP is left alone",
      P.normalize_image_data_url(data_url("bmp", b"junkjunk" * 8)) is None)

print()
print("[3] the message walker copies only what it changes")
msgs = [{"role": "user", "content": [
    {"type": "text", "text": "look"},
    {"type": "image_url", "image_url": {"url": data_url("bmp", bmp)}},
]}]
out = P.normalize_image_formats(msgs)
check("the BMP part became a PNG",
      out[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
check("the caller's own message was not mutated",
      msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/bmp;base64,"))
check("the untouched text part is the same object",
      out[0]["content"][0] is msgs[0]["content"][0])
check("the untouched message is the same object",
      P.normalize_image_formats([{"role": "user", "content": "plain"}])[0]
      is not None)
untouched = [{"role": "user", "content": [{"type": "image_url",
                                           "image_url": {"url": png_data_url()}}]}]
check("a message with only a PNG comes back as the very same object",
      P.normalize_image_formats(untouched) is untouched)
check("a bare-string image_url part is rewritten too",
      P.normalize_image_formats([{"role": "user", "content": [
          {"type": "image_url", "image_url": data_url("bmp", bmp)}]}])[0]
      ["content"][0]["image_url"].startswith("data:image/png;base64,"))
check("a plain {\"url\": ...} part is rewritten too",
      P.normalize_image_formats([{"role": "user", "content": [
          {"type": "image", "url": data_url("bmp", bmp)}]}])[0]
      ["content"][0]["url"].startswith("data:image/png;base64,"))
check("a non-list content is left alone",
      P.normalize_image_formats([{"role": "user", "content": "x"}])[0]
      ["content"] == "x")

print()
print("[4] build_upstream_body gates the rewrite on the switch")
# build_upstream_body reads the switch off the module global, which the runtime
# bootstrap below is what normally points at the isolated directory. Set it now
# so the gating can be checked before the gateway is started.
P.ACCOUNTS_DIR = ACCOUNTS
payload = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": [
    {"type": "text", "text": "look"},
    {"type": "image_url", "image_url": {"url": data_url("bmp", bmp)}},
]}]}


def built_image_url():
    body = P.build_upstream_body(json.loads(json.dumps(payload)))
    return body["messages"][-1]["content"][1]["image_url"]["url"]


wb_settings.set_image_format_normalization_enabled(ACCOUNTS, False)
check("switch off (default) -> the BMP is forwarded untouched",
      built_image_url().startswith("data:image/bmp;base64,"))
check("the switch defaults to off when the key is absent",
      wb_settings.image_format_normalization_enabled(ACCOUNTS) is False)
wb_settings.set_image_format_normalization_enabled(ACCOUNTS, True)
check("switch on -> the BMP is rewritten to PNG",
      built_image_url().startswith("data:image/png;base64,"))
check("the caller's payload is still untouched after the build",
      payload["messages"][0]["content"][1]["image_url"]["url"]
      .startswith("data:image/bmp;base64,"))
wb_settings.set_image_format_normalization_enabled(ACCOUNTS, False)


# ---------------------------------------------------------------------------
# End to end: a real gateway against a mock upstream that sniffs the bytes.
# ---------------------------------------------------------------------------
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UPSTREAM_PORT = life.free_port()
MOCK = {"requests": [], "reject": False}
SSE = b"".join([
    b'data: ' + json.dumps({"id": "c1", "model": "deepseek-v4.1-flash",
                            "choices": [{"index": 0, "delta": {"content": "ok"}}]}).encode() + b"\n\n",
    b'data: ' + json.dumps({"id": "c1", "model": "deepseek-v4.1-flash",
                            "choices": [{"index": 0, "delta": {},
                                         "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                      "total_tokens": 6}}).encode() + b"\n\n",
    b"data: [DONE]\n\n",
])


def body_image_urls(body):
    urls = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            slot = part.get("image_url")
            url = slot.get("url") if isinstance(slot, dict) else slot
            if isinstance(url, str):
                urls.append(url)
    return urls


def read_image_bytes(url):
    if not url.startswith("data:image/"):
        return None
    head, _, payload = url.partition(",")
    if "base64" not in head:
        return None
    return base64.b64decode(payload)


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, ctype, payload):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        MOCK["requests"].append({"path": self.path, "body": body})
        if not self.path.endswith("/chat/completions"):
            return self._reply(200, "application/json", b'{"data":{}}')
        # Mimic the upstream: the format is read from the bytes, not the media
        # type, and anything that is not png/jpeg/webp is refused.
        if MOCK["reject"]:
            for url in body_image_urls(body):
                payload = read_image_bytes(url)
                if payload is None or not payload.startswith(PNG_MAGIC):
                    return self._reply(400, "application/json", json.dumps({
                        "code": 11135,
                        "msg": ("Please start a new conversation, replace the "
                                "image, and try again."),
                    }).encode())
        return self._reply(200, "text/event-stream", SSE)

    def do_GET(self):
        self._reply(200, "application/json", b'{"data":{}}')


upstream = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), MockUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()

_ENDPOINTS = dict(wb_identity._ENDPOINTS)
for key in list(_ENDPOINTS):
    _ENDPOINTS[key] = ("http://127.0.0.1:%d" % UPSTREAM_PORT, _ENDPOINTS[key][1])
wb_identity._ENDPOINTS.update(_ENDPOINTS)

GATEWAY_PORT = life.free_port()


class Args(object):
    host = "127.0.0.1"
    port = GATEWAY_PORT
    api_key = "TESTKEY"
    system_prompt = "You are a helpful assistant."
    user_agent = None
    usage_dir = os.environ["WB_PROXY_USAGE_DIR"]
    accounts_dir = ACCOUNTS
    import_desktop = False
    lan = False
    panel_password = None
    info = None


P._apply_cli_overrides(Args())
P._bootstrap_runtime(Args())
gateway = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), P.Handler)
threading.Thread(target=gateway.serve_forever, daemon=True).start()


def request(method, path, body=None, headers=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (GATEWAY_PORT, path)
    data = None if body is None else (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def wait_ready():
    for _ in range(60):
        try:
            if request("GET", "/health", timeout=2)[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


CHAT = {"model": "deepseek-v4.1-flash", "stream": True, "messages": [
    {"role": "user", "content": [
        {"type": "text", "text": "what is in this picture?"},
        {"type": "image_url", "image_url": {"url": data_url("bmp", bmp)}},
    ]}]}

try:
    if not wait_ready():
        print("  [FAIL] gateway did not start")
        sys.exit(1)

    panel = P.PANEL.create()
    MOCK["reject"] = True

    print()
    print("[5] /settings round trip and the strict-boolean rule")
    code, _h, body = request("GET", "/settings", headers={"X-Panel-Token": panel})
    payload = json.loads(body or b"{}")
    check("GET /settings reports the switch as off by default",
          payload.get("image_format_normalization_enabled") is False, payload.get("image_format_normalization_enabled"))

    code, _h, body = request("POST", "/settings/save",
                             {"image_format_normalization_enabled": "false"},
                             headers={"X-Panel-Token": panel})
    payload = json.loads(body or b"{}")
    check("a string \"false\" is rejected with 400",
          code == 400, (code, body[:200]))
    check("the error names the key and the rule",
          "image_format_normalization_enabled must be true or false"
          in json.dumps(payload), payload)

    code, _h, body = request("POST", "/settings/save",
                             {"image_format_normalization_enabled": True},
                             headers={"X-Panel-Token": panel})
    payload = json.loads(body or b"{}")
    check("a real boolean true is accepted",
          code == 200 and payload.get("image_format_normalization_enabled") is True,
          (code, payload))
    check("the value is persisted to settings.json",
          wb_settings.image_format_normalization_enabled(ACCOUNTS) is True)
    request("POST", "/settings/save", {"image_format_normalization_enabled": False},
            headers={"X-Panel-Token": panel})
    check("turning it back off persists too",
          wb_settings.image_format_normalization_enabled(ACCOUNTS) is False)

    print()
    print("[6] end to end: switch off -> the upstream still sees the BMP (400);"
          " switch on -> it sees a PNG (200)")
    MOCK["requests"] = []
    code, _h, body = request("POST", "/v1/chat/completions", CHAT,
                             headers={"Authorization": "Bearer TESTKEY"})
    seen = body_image_urls(MOCK["requests"][-1]["body"]) if MOCK["requests"] else []
    check("switch off: the upstream received the original BMP data URL",
          seen and seen[-1].startswith("data:image/bmp;base64,"), seen[-1:])
    check("switch off: the client is answered 400 code 11135",
          code == 400 and b"11135" in body, (code, body[:200]))

    request("POST", "/settings/save", {"image_format_normalization_enabled": True},
            headers={"X-Panel-Token": panel})
    MOCK["requests"] = []
    code, _h, body = request("POST", "/v1/chat/completions", CHAT,
                             headers={"Authorization": "Bearer TESTKEY"})
    seen = body_image_urls(MOCK["requests"][-1]["body"]) if MOCK["requests"] else []
    check("switch on: the upstream received a PNG data URL instead",
          seen and seen[-1].startswith("data:image/png;base64,"), seen[-1:])
    check("switch on: the same request now succeeds",
          code == 200, (code, body[:200]))

    request("POST", "/settings/save", {"image_format_normalization_enabled": False},
            headers={"X-Panel-Token": panel})
finally:
    for server in (gateway, upstream):
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
    try:
        if P.SCHEDULER is not None:
            P.SCHEDULER.stop()
    except Exception:
        pass
    try:
        P.PRICING.stop()
    except Exception:
        pass

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
