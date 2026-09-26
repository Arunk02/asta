"""Any corporate web app Asta may drive — configured in one file, not in code.

This began as `solar.py`. It is generic because he asked the obvious question:
*"tmr even if move to other teams also it should be easy for me to configure"*.
A door shaped around one system is a door that has to be rewritten the day the
system changes, and the interesting parts — which environments may be written
to, how to sign in, how to read a page, what a field expects — are the same for
Solar, Kevin, LaunchMate or whatever comes next.

**Configured in `data/sites.json`, which is gitignored.** Same place and same
reason as `data/temporal-envs.json`: his hostnames are not in this public
repository, and nothing here logs or echoes them.

    {
      "solar": {
        "label": "Solar · inland bookings",
        "envs": {"sit": "https://…", "uat": "https://…", "prod": "https://…"},
        "write": ["sit", "uat"],
        "landing": "/inland/booking/",
        "http1": true,
        "knowledge": ["NAM_Inland_Booking", "TelikosE2EFlow"]
      }
    }

Everything below follows from four rules, and each exists because of something
that actually happened.

  EVERY LISTED ENVIRONMENT MAY BE READ; ONLY `write` MAY BE CHANGED, AND NEVER
  PRODUCTION. He reads production to debug and has no write access there. The
  first version refused production outright and took away something real to
  prevent something impossible. Production being unwritable is enforced in code,
  not by being left off a list, because the list is his and that rule is not.

  READING CANNOT TOUCH. `look` has no click, no fill, no submit. That is what
  makes it safe to point at production, and a test asserts the absence of each.

  A QUIRK BELONGS TO ITS SITE. Solar's API calls die with
  net::ERR_HTTP2_PROTOCOL_ERROR in this browser, so Solar asks for HTTP/1.1.
  The next site should not inherit that; `http1` is per site.

  WHAT A FIELD EXPECTS COMES FROM HIS OWN DOCUMENTS, WITH A CITATION. Filling a
  booking form from imagination is the automation that confidently submits
  something wrong. `guidance` asks the knowledge folder (P13) and hands back the
  passage and where it came from, so a value can be checked before it is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

#: Production, however it is spelled. Readable; never writable.
_PRODUCTION = re.compile(r"\b(prod|production|live)\b", re.I)


def config_path() -> Path:
    from . import store
    return Path(store.DB_PATH).parent / "sites.json"


def all_sites() -> dict:
    """Everything configured, or {} — a missing or broken file is not a crash."""
    try:
        data = json.loads(config_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:                                           # noqa: BLE001
        return {}


def site(name: str) -> dict:
    return all_sites().get((name or "").strip().lower(), {})


def envs(name: str) -> dict[str, str]:
    """{env: base url} for a site — every one he listed, production included."""
    found = site(name).get("envs") or {}
    return {str(k).lower(): str(v).rstrip("/") for k, v in found.items() if k and v}


def base_for(name: str, env: str) -> str:
    return envs(name).get((env or "").strip().lower(), "")


def allowed(name: str, url: str) -> bool:
    """May Asta OPEN this URL? True for any environment of this site."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:                                           # noqa: BLE001
        return False
    return bool(host) and any(
        host == urlparse(base).netloc.lower() for base in envs(name).values())


def writable(name: str, env: str) -> bool:
    """May Asta CHANGE anything here? Only where he said so, and never in prod."""
    env = (env or "").strip().lower()
    if not env or env not in envs(name):
        return False
    if _PRODUCTION.search(env) or _PRODUCTION.search(envs(name).get(env, "")):
        return False
    return env in {str(x).lower() for x in (site(name).get("write") or [])}


def configured(name: str = "") -> str:
    """'' when usable, otherwise what is missing, in his words."""
    if not all_sites():
        return (f"No sites configured — create {config_path()} with one entry per "
                f"system: its environments, and which of them may be written to. "
                f"Production can be read and never written.")
    if name and not site(name):
        return f"'{name}' is not configured — known: {', '.join(sorted(all_sites()))}"
    return ""


def health(name: str = "") -> dict:
    """What each site can do. Environment NAMES only — never their URLs."""
    from . import store
    out = []
    for key in sorted(all_sites()):
        if name and key != name.lower():
            continue
        known = envs(key)
        out.append({
            "site": key, "label": site(key).get("label", key),
            "environments": sorted(known),
            "writable": sorted(e for e in known if writable(key, e)),
            "read_only": sorted(e for e in known if not writable(key, e)),
            "signed_in": sorted(e for e in known
                                if store.kv_get(f"site_session:{key}:{e}")),
            "http1": bool(site(key).get("http1")),
        })
    return {"configured": bool(out), "sites": out, "hint": configured(name) or "ready",
            "file": str(config_path())}


# --- reading a page ----------------------------------------------------------

_LOOK = """() => {
  const t = (s, n) => Array.from(document.querySelectorAll(s))
      .map(e => (e.innerText || e.getAttribute('aria-label') || e.title || '').trim())
      .filter(x => x && x.length < 80).slice(0, n);
  const table = document.querySelector('table');
  const cells = tr => Array.from(tr.querySelectorAll('td')).slice(0, 10)
      .map(td => td.innerText.replace(/\\u200c/g, '').trim().slice(0, 30));
  return {
    title: document.title,
    heading: t('h1,h2', 4),
    buttons: t('button,[role=button],a[class*=btn]', 30),
    fields: Array.from(document.querySelectorAll('input,select,textarea'))
        .map(e => e.name || e.id || e.getAttribute('aria-label') || e.placeholder || e.type)
        .filter(Boolean).slice(0, 40),
    // What each control is FOR, not what it is called in the build output. A
    // react-select is an input named react-select-2-input and a label saying
    // "Filter by" three elements up; the id is useless to a person and to a
    // brain, and the words beside it are what both would reach for.
    controls: Array.from(document.querySelectorAll(
        'input,select,textarea,[role=combobox],[role=listbox]')).map(e => {
      let near = '';
      if (e.id) {
        const l = document.querySelector(`label[for="${e.id}"]`);
        if (l) near = l.innerText.trim();
      }
      let up = e.closest('label') || e.parentElement;
      for (let i = 0; !near && up && i < 4; i++, up = up.parentElement) {
        const txt = (up.innerText || '').replace(/\u200c/g, '').trim();
        if (txt && txt.length < 90) near = txt;
      }
      return {as: e.tagName.toLowerCase(), name: e.name || e.id || '',
              label: e.getAttribute('aria-label') || '', placeholder: e.placeholder || '',
              beside: near.split(String.fromCharCode(10))[0].slice(0, 60)};
    }).slice(0, 25),
    columns: table ? Array.from(table.querySelectorAll('thead th,thead td'))
        .map(e => e.innerText.trim()).filter(Boolean) : [],
    rows: table ? table.querySelectorAll('tbody tr').length : 0,
    filled_rows: table ? Array.from(table.querySelectorAll('tbody tr'))
        .map(cells).filter(r => r.some(c => c)).slice(0, 5) : [],
    text: (document.body.innerText || '').replace(/\\u200c/g, '').slice(0, 6000),
    links: Array.from(document.querySelectorAll('a[href]'))
        .map(a => a.getAttribute('href')).filter(h => h && !h.startsWith('#'))
        .filter((h, i, all) => all.indexOf(h) === i).slice(0, 40)
  };
}"""


async def look(name: str, env: str = "", path: str = "", settle_ms: int = 12000) -> dict:
    """What is on a page right now. Reads; clicks nothing.

    Safe on any environment he listed, production included, because it cannot
    change anything.
    """
    missing = configured(name)
    if missing:
        return {"error": missing}
    known = envs(name)
    env = (env or next(iter(known), "")).lower()
    base = known.get(env, "")
    if not base:
        return {"error": f"'{env}' is not configured for {name} — "
                         f"allowed: {', '.join(known) or '(none)'}"}
    where = path or site(name).get("landing") or ""
    url = base + (where if where.startswith("/") else f"/{where}" if where else "")
    if not allowed(name, url):
        return {"error": "that URL is outside the environments he allowed"}
    last = ""
    for attempt in (1, 2):
        try:
            return await _look_once(name, env, base, url, settle_ms)
        except Exception as exc:                                # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:200]
            if attempt == 1:
                # The shared browser can be taken away mid-read: Teams' own
                # recovery closes the pool when it thinks the session is wedged,
                # and it does not know another tab is open in it.
                from . import teams_bridge
                await teams_bridge.close_pool()
    return {"error": f"could not read the page — {last}", "site": name, "env": env}


def _watchers() -> tuple[list, object, object]:
    """Collectors for what the page asked for and did not get.

    A watcher on responses alone reported nothing while the screen said "Error
    in fetching bookings": those requests never got a response at all, and there
    is no `response` event for a connection that dies.
    """
    failed: list[dict] = []

    def _path_of(url: str) -> str:
        plain = url.split("?", 1)[0]
        return plain[plain.find("/", 9):][:140] if "//" in plain else plain[:140]

    def on_response(response) -> None:
        with contextlib.suppress(Exception):
            if response.status >= 400:
                failed.append({"status": response.status, "call": _path_of(response.url)})

    def on_failed(request) -> None:
        with contextlib.suppress(Exception):
            failed.append({"status": (request.failure or "failed")[:60],
                           "call": _path_of(request.url)})

    return failed, on_response, on_failed


def _dedupe(failed: list[dict]) -> list[dict]:
    """One broken call retried nine times is one fault."""
    seen, out = set(), []
    for f in failed:
        mark = (f["status"], f["call"])
        if mark not in seen:
            seen.add(mark)
            out.append(f)
    return out[:10]


async def _look_once(name: str, env: str, base: str, url: str, settle_ms: int) -> dict:
    from . import teams_bridge
    failed, on_response, on_failed = _watchers()
    async with teams_bridge.site_page(url, watch=on_response, failed_watch=on_failed) as page:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=max(4000, int(settle_ms)))
        await page.wait_for_timeout(2500)
        out = await page.evaluate(_LOOK)
        if failed:
            out["failed_calls"] = _dedupe(failed)
        out["signed_in"] = "microsoftonline" not in page.url and "/login" not in page.url.lower()
        out["site"], out["env"] = name, env
        # The path only — his hostnames stay out of anything that gets logged.
        out["path"] = page.url[len(base):] if page.url.startswith(base) else "(elsewhere)"
    return out


# --- choosing a filter, and filling a form ------------------------------------

#: How a control is found: by the words next to it, never by a coordinate and
#: never by a generated class name. A label is the part of a page its own team
#: reworded least, and a coordinate is wrong the moment anything above it moves.
_FIND = """(label) => {
  const want = label.toLowerCase();
  const near = el => {
    const bits = [el.getAttribute('aria-label'), el.getAttribute('placeholder'),
                  el.name, el.id, el.title];
    const own = bits.filter(Boolean).join(' ').toLowerCase();
    if (own.includes(want)) return true;
    // The label element that points at it, or the one wrapping it.
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l && l.innerText.toLowerCase().includes(want)) return true;
    }
    let up = el.closest('label') || el.parentElement;
    for (let i = 0; up && i < 3; i++, up = up.parentElement) {
      const txt = (up.innerText || '').toLowerCase();
      if (txt.length < 120 && txt.includes(want)) return true;
    }
    return false;
  };
  const all = Array.from(document.querySelectorAll('input,select,textarea,[role=combobox]'));
  const hit = all.find(near);
  if (!hit) return {found: false,
                    offered: all.map(e => e.getAttribute('aria-label') || e.name ||
                                          e.id || e.placeholder || e.type)
                               .filter(Boolean).slice(0, 30)};
  hit.setAttribute('data-asta-target', '1');
  return {found: true, tag: hit.tagName.toLowerCase(), type: hit.type || '',
          options: hit.tagName === 'SELECT'
              ? Array.from(hit.options).map(o => o.text.trim()).slice(0, 40) : []};
}"""


async def controls(name: str, env: str = "", path: str = "",
                  settle_ms: int = 12000) -> dict:
    """Every control on a page, by the words that identify it.

    What to call a field, before anything tries to fill one. A page's own labels
    are the stable part; a generated class name is not.
    """
    out = await look(name, env, path, settle_ms)
    if out.get("error"):
        return out
    return {"site": name, "env": out.get("env"), "path": out.get("path"),
            "fields": out.get("fields") or [], "buttons": out.get("buttons") or [],
            "columns": out.get("columns") or []}


async def choose(name: str, env: str, field: str, value: str,
                 path: str = "", settle_ms: int = 12000) -> dict:
    """Set a LIST FILTER and report what the list then shows.

    A filter changes what he is looking at, not what the system holds, so this
    is allowed wherever reading is — including production, where narrowing a
    list is exactly how a live problem gets found. It reads the control back
    afterwards: a filter that silently did not take is a page he would then
    read as "no results".
    """
    missing = configured(name)
    if missing:
        return {"error": missing}
    base = base_for(name, env)
    if not base:
        return {"error": f"'{env}' is not configured for {name}"}
    where = path or site(name).get("landing") or ""
    url = base + (where if where.startswith("/") else f"/{where}" if where else "")
    if not allowed(name, url):
        return {"error": "that URL is outside the environments he allowed"}
    from . import teams_bridge
    failed, on_response, on_failed = _watchers()
    async with teams_bridge.site_page(url, watch=on_response,
                                     failed_watch=on_failed) as page:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=max(4000, int(settle_ms)))
        await page.wait_for_timeout(2000)
        found = await page.evaluate(_FIND, field)
        if not found.get("found"):
            return {"error": f"no control matching {field!r} on that page",
                    "offered": found.get("offered", []), "site": name, "env": env}
        target = page.locator("[data-asta-target='1']").first
        with contextlib.suppress(Exception):
            if found.get("tag") == "select":
                await target.select_option(label=value)
            else:
                await target.fill(value)
                # A react-select needs the keyboard, not a value: typing opens
                # the menu and Enter takes the highlighted option.
                await target.press("Enter")
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(2500)
        out = await page.evaluate(_LOOK)
        became = await target.input_value() if found.get("tag") != "select" else value
        return {"site": name, "env": env, "field": field, "asked": value,
                "became": became, "took": bool(became and value.lower() in became.lower()),
                "rows": out.get("rows"), "showing": out.get("filled_rows") or [],
                "columns": out.get("columns") or [],
                **({"failed_calls": _dedupe(failed)} if failed else {})}


async def plan_fill(name: str, env: str, values: dict, path: str = "") -> dict:
    """What filling this form WOULD do, checked against his own documents.

    Nothing is typed and nothing is submitted. Every value comes back with what
    his knowledge folder says about that field, so he is approving a form he can
    check rather than a promise. `submit` is the separate, gated step.
    """
    missing = configured(name)
    if missing:
        return {"error": missing}
    if not writable(name, env):
        return {"error": f"{name} {env} is read-only — "
                         f"writable: {', '.join(sorted(e for e in envs(name) if writable(name, e))) or '(none)'}"}
    checked = []
    for field, value in (values or {}).items():
        says = await guidance(field, name)
        checked.append({"field": field, "value": value,
                        "documented": says.get("found", False),
                        "cite": says.get("cite", ""),
                        "says": (says.get("passages") or [{}])[0].get("text", "")[:300]
                        if says.get("found") else says.get("say", "")})
    unknown = [c["field"] for c in checked if not c["documented"]]
    return {"site": name, "env": env, "path": path, "fields": checked,
            "undocumented": unknown, "submitted": False,
            "say": ("Nothing is typed and nothing is submitted. "
                    + (f"{len(unknown)} field(s) are not described in his documents: "
                       f"{', '.join(unknown)}." if unknown
                       else "Every field is described in his documents."))}


# --- what a field expects, from his own documents -----------------------------

async def guidance(field: str, name: str = "") -> dict:
    """What his documents say a field expects, with the citation.

    A form filled from imagination is the automation that confidently submits
    something wrong. This is the alternative: the passage and where it came
    from, so a value can be checked before it is used.
    """
    from . import knowledge
    hint = " ".join(str(x) for x in (site(name).get("knowledge") or [])) if name else ""
    with contextlib.suppress(Exception):
        knowledge.reindex()
    hits = knowledge.search(f"{field} {hint}".strip(), 3)
    if not hits:
        return {"field": field, "found": False,
                "say": f"His documents say nothing about {field!r} — "
                       f"searched {knowledge.folder()}."}
    return {"field": field, "found": True,
            "passages": [{"document": h["document"], "where": h["where"],
                          "text": h["text"][:700]} for h in hits],
            "cite": knowledge.cite(hits)}


async def login(name: str, env: str = "") -> str:
    """Open a site in a visible window so he completes the SSO himself."""
    missing = configured(name)
    if missing:
        return missing
    known = envs(name)
    env = (env or next(iter(known), "")).lower()
    url = known.get(env, "")
    if not url:
        return f"'{env}' is not configured for {name} — allowed: {', '.join(known) or '(none)'}"
    from . import store, teams_bridge
    print(f"Opening {name} {env} — complete any sign-in in the window.")
    pw, ctx = await teams_bridge._launch(headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print("Waiting up to 5 minutes; press Ctrl-C when you are done.")
        with contextlib.suppress(Exception):
            await page.wait_for_timeout(300000)
        signed = "microsoftonline" not in page.url and "/login" not in page.url.lower()
        store.kv_set(f"site_session:{name}:{env}", "1" if signed else "")
        return (f"{name} {env}: signed in, session saved." if signed
                else f"{name} {env}: still on a sign-in page — not saved.")
    finally:
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    from . import store
    store.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "health"
    if cmd == "login":
        print(asyncio.run(login(sys.argv[2] if len(sys.argv) > 2 else "",
                                sys.argv[3] if len(sys.argv) > 3 else "")))
    else:
        print(json.dumps(health(sys.argv[2] if len(sys.argv) > 2 else ""), indent=1))
