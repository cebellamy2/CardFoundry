"""The credential every scheduled job and the pre-push hook uses to
authenticate to the app.

Machines have their own secret, separate from any human login, so either
can be rotated without breaking the other and a cron leaking its secret
does not hand over a person's access.

THE LEGACY FALLBACK IS GONE (Slice 2 Stage B, v2.0.0). Stage A read
CARDFOUNDRY_SERVICE_PASSWORD and fell back to the retiring shared
password, which is what made that deploy safe in either order. All six
crons were then confirmed authenticating on the service credential from
their own logs, and only then was the fallback -- and the shared password
itself -- removed. Removing it is the point: a fallback nobody has
noticed is still in use is a credential nobody knows they depend on.

NOTHING HERE EVER PRINTS, LOGS OR RETURNS A SECRET FOR DISPLAY. It
reports which VARIABLE it used, by name, and the caller prints that.
"""

import os

SERVICE_PASSWORD_VAR = "CARDFOUNDRY_SERVICE_PASSWORD"

# The Basic-auth username a machine presents. Load-bearing since Stage A:
# the gate accepts the service credential only for a username in
# main.SERVICE_USERNAMES, of which "hook" (the pre-push guard) is the
# other member.
SERVICE_USERNAME = "cron"


class MissingServiceCredential(RuntimeError):
    """The variable is not set. Raised rather than defaulted to empty: an
    empty password would produce a puzzling 401 per tick instead of one
    clear message naming exactly what to set, and since Stage B there is
    no other credential for it to fall back to."""


def service_password() -> str:
    password = os.environ.get(SERVICE_PASSWORD_VAR, "")
    if not password:
        raise MissingServiceCredential(
            f"{SERVICE_PASSWORD_VAR} is not set. Set it on this Railway "
            "service -- as a reference to the CardFoundry service's own "
            "value, so the secret itself lives in exactly one place."
        )
    return password


def service_auth(username: str = SERVICE_USERNAME) -> tuple[str, str]:
    """(username, password), shaped for httpx's `auth=` argument.

    Prints the variable NAME it used, never the value -- one line per
    tick, which is how a credential change is confirmed per service from
    the cron's own log.
    """
    password = service_password()
    print(f"Auth: using {SERVICE_PASSWORD_VAR}.")
    return username, password
