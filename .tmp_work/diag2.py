import sys, time
sys.path.insert(0, "/Users/a1/Code/Linuxdoday")
from browser_utils import start_browser, BASE
ctx, page = start_browser(headless=False, offscreen=False)
try:
    try: page.goto(BASE, timeout=45000)
    except Exception as e: print("goto err:", e)
    page.wait_for_load_state("domcontentloaded"); time.sleep(3)
    print("TITLE:", page.title())
    print("LOGIN(#current-user):", page.locator("#current-user").count())
    st = page.evaluate("""async () => { try { const r = await fetch("/c/develop/4.json?page=0",{credentials:"include",headers:{Accept:"application/json"}}); return r.status;} catch(e){return "ERR:"+e;} }""")
    print("JSON(/c/develop/4):", st)
finally:
    ctx.close()
