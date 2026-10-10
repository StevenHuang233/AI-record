"""额外只读资源根（--asset-root）的 HTTP 层测试：起一个真实服务，验证播放、Range、越界、软链逃逸。
用法：python3 tests/test_asset_roots_http.py"""
import os, sys, json, time, shutil, tempfile, subprocess, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ext = tempfile.mkdtemp(prefix="ext-"); secret = tempfile.mkdtemp(prefix="sec-"); state = tempfile.mkdtemp(prefix="st-")
os.makedirs(os.path.join(ext, "videos")); open(os.path.join(ext, "videos", "a.mp4"), "wb").write(b"\x00\x00\x00\x18ftypisom" + b"x" * 5000)
open(os.path.join(secret, "pw.png"), "wb").write(b"SECRET")
os.symlink(secret, os.path.join(ext, "escape"))
port = 38612
env = dict(os.environ, SB_STATE_DIR=state)
p = subprocess.Popen([sys.executable, "server.py", "--port", str(port), "--asset-root", f"rel={ext}", "--asset-root", f"bad=/no/such"],
                     cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2.5)
def get(path, rng=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Range": rng} if rng else {})
    try:
        r = urllib.request.urlopen(req, timeout=10); return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, b""
ok = True
def check(name, cond, extra=""):
    global ok; ok &= bool(cond); print(("ok  " if cond else "FAIL"), name, extra)
st, h, b = get("/assets/rel/videos/a.mp4", "bytes=0-99")
check("额外根里的视频能播（206 + video/mp4 + Range）", st == 206 and h.get("Content-Type") == "video/mp4" and len(b) == 100 and h.get("Content-Range", "").startswith("bytes 0-99/"), f"{st} {h.get('Content-Type')} {h.get('Content-Range')}")
st, h, b = get("/assets/rel/videos/a.mp4"); check("整文件 200", st == 200 and len(b) == 5012, f"{st} {len(b)}")
check("软链逃逸被拒（404，不泄露内容）", get("/assets/rel/escape/pw.png")[0] == 404)
check("`..` 被拒", get("/assets/rel/%2e%2e/etc/passwd")[0] == 404 and get("/assets/rel/videos/%2e%2e/%2e%2e/x.mp4")[0] == 404)
check("未登记的别名当成主根下的路径（404）", get("/assets/zzz/videos/a.mp4")[0] == 404)
check("无效登记（目录不存在）不生效", get("/assets/bad/anything.mp4")[0] == 404)
check("扩展名白名单仍生效（.txt 不给）", get("/assets/rel/videos/a.txt")[0] == 404)
time.sleep(0.3); p.terminate(); out = p.communicate(timeout=5)[0]
check("启动日志列出别名、不打印真实路径", "额外只读资源根=rel" in out and ext not in out, out.strip().splitlines()[0][:100] if out.strip() else "")
check("无效登记启动时有警告", "额外资源根「bad」无效" in out)
for d in (ext, secret, state): shutil.rmtree(d, ignore_errors=True)
print("全部通过" if ok else "有失败"); sys.exit(0 if ok else 1)
