import sys, time
sys.path.insert(0, "/Users/a1/Code/Linuxdoday")
from browser_utils import start_browser
ctx, page = start_browser(headless=False, offscreen=False)
try:
    page.goto("https://linux.do/", timeout=45000, wait_until="domcontentloaded")
    time.sleep(4)
    print("title:", page.title())
    info = page.evaluate("""async () => {
      const out = {};
      try { const r = await fetch("/session/current.json", {credentials:"include", headers:{Accept:"application/json"}});
            out.status = r.status; out.body = r.ok ? await r.json() : (await r.text()).slice(0,150); } catch(e){ out.err = ""+e; }
      return out;
    }""")
    print("session status:", info.get("status"))
    b = info.get("body")
    if isinstance(b, dict):
        u = b.get("current_user") or {}
        print("USER:", u.get("username"), "id=", u.get("id"))
    else:
        print("body:", str(b)[:150])
    names=[c["name"] for c in ctx.cookies()]
    print("has _forum_session:", any("_forum_session" in n for n in names))
finally:
    ctx.close()
