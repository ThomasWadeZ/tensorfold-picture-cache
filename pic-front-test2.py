#!/usr/bin/env python3
"""Picture frontend cost on v1.4: images cached to disk, plus a text-only control with the same token count."""
import base64, json, os, struct, sys, time, urllib.request, zlib
sys.path.insert(0, "/home/a10/src/GLM-tf-v1.4/tools")
API = "http://127.0.0.1:8888"
W, H = 1920, 1080
DIR = "/tmp/pic-cache-test"

def png(seed):
    rows = bytearray()
    for y in range(H):
        r = (seed * 37 + y) % 256
        g = (seed * 61 + (y // 3)) % 256
        b = (seed * 97 + (y // 7)) % 256
        rows += b"\x00" + bytes((r, g, b)) * W
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")

def png_bytes(seed):
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, "img-%03d.png" % seed)
    if os.path.exists(p):
        return open(p, "rb").read()
    d = png(seed)
    open(p, "wb").write(d)
    return d

def health():
    return json.loads(urllib.request.urlopen(API + "/health", timeout=30).read())

def ask(messages, mx=16):
    body = {"model": "GLM-5.3-Flash-EXL3", "messages": messages, "max_tokens": mx, "temperature": 0,
            "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(API + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter(); ttft = None; usage = None
    for line in urllib.request.urlopen(req, timeout=3600):
        line = line.decode().strip()
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p == "[DONE]":
            break
        d = json.loads(p)
        if d.get("usage"):
            usage = d["usage"]
        ch = (d.get("choices") or [{}])[0]
        if (ch.get("delta") or {}).get("content") and ttft is None:
            ttft = time.perf_counter() - t0
    return {"ttft": ttft, "usage": usage}

def image_parts(n):
    out = [{"type": "text", "text": "看下面 %d 张图，只回答第一张的主色，五个字以内。" % n}]
    for i in range(n):
        out.append({"type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes(i + 1)).decode()}})
    return out

def measure(label, content):
    c = [{"role": "user", "content": content}]
    a = health(); r1 = ask(c); b = health()
    pre1 = b.get("prefill_seconds_total", 0) - a.get("prefill_seconds_total", 0)
    c2 = c + [{"role": "assistant", "content": "好"}, {"role": "user", "content": "再确认一次，只回答 OK"}]
    a2 = health(); r2 = ask(c2); b2 = health()
    pre2 = b2.get("prefill_seconds_total", 0) - a2.get("prefill_seconds_total", 0)
    tok = (r2["usage"] or {}).get("prompt_tokens", -1)
    front = (r2["ttft"] or 0) - pre2 - 0.15
    print("  %-22s 提示 %7d token | 冷轮 %7.2fs（预填 %7.2fs）| 热轮 %6.2fs（预填 %5.2fs）-> 前端≈ %5.2fs" % (
        label, tok, r1["ttft"] or -1, pre1, r2["ttft"] or -1, pre2, front))
    return front, tok

if __name__ == "__main__":
    from client import prose
    h = health()
    print("  引擎: 请求 %s 存档 %s" % (h.get("requests_total"), h.get("kept_prompts")))
    res = {}
    for n in [int(x) for x in sys.argv[1:]] or [10, 70]:
        res[n] = measure("%d 张图" % n, image_parts(n))
    # 同 token 数的纯文字对照：把图片带来的分词/预填之外的固定开销刨掉
    target = res[max(res)][1]
    text = prose(int(target * 0.9), 5)
    res["text"] = measure("纯文字对照", [{"type": "text", "text": text + "\n只回答 OK"}])
    ks = sorted(k for k in res if isinstance(k, int))
    if len(ks) > 1:
        slope = (res[ks[-1]][0] - res[ks[0]][0]) / (ks[-1] - ks[0])
        print("  每张图前端 ≈ %.1f ms；纯文字同 token 数的前端 ≈ %.2fs（这部分是分词等固定开销）" % (
            slope * 1000, res["text"][0]))
