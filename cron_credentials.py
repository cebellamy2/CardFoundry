"""The credential every scheduled job and the pre-push hook uses to
authenticate to the app.

Slice 2, Stage A: machines get their OWN secret, separate from any human
login, so either can be rotated without breaking the other. Before this
they sent the shared site password -- the same string a person typed
into a browser prompt -- which meant rotating it broke six crons, and a
cron leaking it handed over a human's access too.

READS THE NEW VARIABLE, FALLS BACK TO THE OLD ONE. That fallback is the
whole reason Stage A is safe to deploy in either order: the code can ship
before CARDFOUNDRY_SERVICE_PASSWORD exists in Railway, or after, and no
tick fails either way. Stage B removes the fallback along with the shared
password itself.

NOTHING HERE EVER PRINTS, LOGS OR RETURNS A SECRET FOR DISPLAY. It
reports which VARIABLE it used, by name, and the caller prints that --
so "is this cron on the new credential yet?" is answerable from a log
without the value ever appearing anywhere.
"""

import os

SERVICE_PASSWORD_VAR = "CARDFOUNDRY_SERVICE_PASSWORD"
LEGACY_PASSWORD_VAR = "CARDFOUNDRY_ADMIN_PASSWORD"

# The Basic-auth username a machine presents. The gate has always thrown
# the username away; from Stage A on it is load-bearing -- the service
# credential is only accepted for a username in main.SERVICE_USERNAMES,
# and "hook" (the pre-push guard) is the other member.
SERVICE_USERNAME = "cron"


class MissingServiceCredential(RuntimeError):
    """Neither variable is set. Raised rather than defaulted to empty:
    an empty password would produce a puzzling 401 per tick instead of
    one clear message naming what to set."""


def service_password() -> tuple[str, str]:
    """Returns (password, variable_name_it_came_from).

    The caller prints the NAME, never the value.
    """
    new = os.environ.get(SERVICE_PASSWORD_VAR, "")
    if new:
        return new, SERVICE_PASSWORD_VAR
    legacy = os.environ.get(LEGACY_PASSWORD_VAR, "")
    if legacy:
        return legacy, LEGACY_PASSWORD_VAR
    raise MissingServiceCredential(
        f"Neither {SERVICE_PASSWORD_VAR} nor {LEGACY_PASSWORD_VAR} is set. "
        f"Set {SERVICE_PASSWORD_VAR} on this Railway service."
    )


def service_auth(username: str = SERVICE_USERNAME) -> tuple[str, str]:
    """(username, password), shaped for httpx's `auth=` argument.

    Also prints which variable supplied it -- one line per tick, naming
    a variable and never a value. This is how Stage A's rollout is
    verified per service: a tick still saying LEGACY has not picked up
    the new secret yet.
    """
    password, source = service_password()
    if source == LEGACY_PASSWORD_VAR:
        print(
            f"Auth: using {LEGACY_PASSWORD_VAR} (LEGACY fallback) -- "
            f"set {SERVICE_PASSWORD_VAR} on this service to move off it."
        )
    else:
        print(f"Auth: using {SERVICE_PASSWORD_VAR}.")
    return username, password
