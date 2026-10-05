#!/usr/bin/env python3
"""Same picture, same question, twice: the first read is a miss (decoded), the second a hit. Replies must match."""
import base64, json, sys, time, urllib.request
sys.path.insert(0, "/tmp")
from importlib.machinery import SourceFileLoader
t = SourceFileLoader("t", "/tmp/pic-front-test2.py").load_module()
API = "http://127.0.0.1:8888"

def ask(messages, mx=64):
    body = {"model": "GLM-5.3-Flash-EXL3", "messages": messages, "max_tokens": mx, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(API + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    return d["choices"][0]["message"].get("content") or "", d["usage"]

img = "data:image/png;base64," + base64.b64encode(t.png_bytes(3)).decode()
q = "这张图从上到下有几种主要颜色？按顺序列出颜色名，逗号分隔。"
msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": img}},
                                     {"type": "text", "text": q}]}]
r1, u1 = ask(msgs)
r2, u2 = ask(msgs)
r3, u3 = ask(msgs)
print("  第 1 发（未命中，现解码）: %d 提示 token | %s" % (u1["prompt_tokens"], r1[:60].replace("\n", " ")))
print("  第 2 发（命中缓存）      : %d 提示 token | %s" % (u2["prompt_tokens"], r2[:60].replace("\n", " ")))
print("  第 3 发（命中缓存）      : %d 提示 token | %s" % (u3["prompt_tokens"], r3[:60].replace("\n", " ")))
ok = (r1 == r2 == r3) and (u1["prompt_tokens"] == u2["prompt_tokens"] == u3["prompt_tokens"])
print("  结论: %s" % ("一致 ✓（缓存路径与解码路径产出同一提示与同一答案）" if ok else "★ 不一致，需排查"))
