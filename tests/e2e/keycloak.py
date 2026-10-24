"""The browser side of a Keycloak login to MISP, run in the sync container of a test stack.

MISP and the browser reach Keycloak under one name, so the issuer matches only inside
the compose network. The sync container is read-only like the Kubernetes Jobs, so the
cookies stay in memory.
"""

import json

FLOW = """
import html, http.cookiejar, json, re, sys, urllib.error, urllib.parse, urllib.request
jar = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
follow = urllib.request.build_opener(jar)
stay = urllib.request.build_opener(jar, Stay)
def open_(opener, url, data=None, accept=None):
    request = urllib.request.Request(url, data=data, headers={"Accept": accept} if accept else {})
    try:
        return opener.open(request, timeout=30)
    except urllib.error.HTTPError as e:
        return e
auth = open_(stay, "http://caddy:8080/users/login?OidcAuth=enable").headers.get("Location", "")
print("auth=" + auth)
login = urllib.parse.urlencode({"username": sys.argv[1], "password": sys.argv[2]}).encode()
# With organisations on, Keycloak asks for the username first and the password on a second page
final = open_(follow, auth)
for _ in range(3):
    form = re.search(r'<form id="kc-form-login"[^>]*action="([^"]+)"', final.read().decode())
    if not form:
        break
    print("form=" + html.unescape(form.group(1)))
    final = open_(follow, html.unescape(form.group(1)), data=login)
print(f"final={final.geturl()} {final.status}")
me = open_(follow, "http://caddy:8080/users/view/me.json", accept="application/json").read()
try:
    print(json.dumps(json.loads(me)))
except ValueError:
    print("{}")
"""


def login(stack, username: str, password: str) -> dict:
    """Log in through MISP's OidcAuth and Keycloak's form: the flow's output and the user MISP shows.

    Mixed auth needs ?OidcAuth=enable to start the flow; the callback lands on
    http://caddy:8080/users/login.
    """
    _, out = stack.run("sync", "python3", "-c", FLOW, username, password)
    user = next((json.loads(line) for line in reversed(out.splitlines()) if line.startswith("{")), {})
    return {"out": out, "user": user}
