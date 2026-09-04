"""
Login accounts: two roles (admin, standard), name + 4-digit PIN.

An account's pin_hash is NULL in exactly two situations - a brand-new
account (self-signup or admin-created) and one an admin just reset - and
both land on the same "choose your PIN" stage of app.py's single /login
route. There is no separate "temporary password" concept.

g.user holds the current request's logged-in user (or None), loaded once per
request in app.py's before_request hook and exposed to every template as
`current_user` - never as `session`, since upload.html already uses that
template name for the upload-batch DB row (session['job_number']), which
would otherwise collide with Flask's own injected `session` global.
"""
from functools import wraps

from flask import abort, g, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import db
from config import ADMIN_NAME, ADMIN_PIN, LOGIN_MAX_ATTEMPTS, update_admin_pin_in_env


def hash_pin(pin):
    return generate_password_hash(pin)


def verify_pin(pin, pin_hash):
    return bool(pin_hash) and check_password_hash(pin_hash, pin)


def set_pin_and_sync(user_id, pin):
    """The one place a user's PIN gets set - first-time choice, post-reset
    choice, or a logged-in self-service change (see app.py's /login and
    /change-pin). If this is the bootstrap Admin account, also mirrors the
    new PIN into .env in plaintext (see config.update_admin_pin_in_env) -
    that account is meant to be recoverable by reading .env, since there's
    no other admin to reset it for you if you're the only one. Returns the
    updated user row."""
    db.set_pin(user_id, hash_pin(pin))
    user = db.get_user(user_id)
    if ADMIN_NAME and user["name_norm"] == ADMIN_NAME.strip().casefold():
        update_admin_pin_in_env(pin)
    return user


def ensure_bootstrap_admin():
    """Creates the first Admin account from .env on startup, if it doesn't
    already exist yet. Only ever CREATES - never overwrites an existing
    account's PIN - so changing ADMIN_PIN later or logging in and changing it
    yourself doesn't get clobbered by the next restart. Silently does
    nothing if ADMIN_NAME/ADMIN_PIN aren't set (e.g. local dev without a
    configured .env)."""
    if not ADMIN_NAME or not ADMIN_PIN:
        print("[startup] ADMIN_NAME/ADMIN_PIN not set in .env - no admin account bootstrapped.")
        return
    if db.find_user_by_name(ADMIN_NAME):
        return
    db.create_user(ADMIN_NAME, role="admin", pin_hash=hash_pin(ADMIN_PIN))
    print(f"[startup] Bootstrapped admin account '{ADMIN_NAME}'.")


def load_current_user():
    """Called from app.py's before_request. Only a fully completed login
    (login_user below) ever sets session['user_id'] - a login still in
    progress (e.g. mid choose-PIN stage) carries its state in hidden form
    fields instead, never in the session, so it can't be mistaken for a
    real logged-in session."""
    user_id = session.get("user_id")
    g.user = db.get_user(user_id) if user_id else None


def login_user(user_row):
    session.clear()
    session.permanent = True  # see config.SESSION_LIFETIME_DAYS - without this the
    # cookie has no expiry set at all and browsers drop it the moment the phone's
    # browser process ends, not just on an actual logout.
    session["user_id"] = user_row["id"]
    g.user = user_row


def logout_user():
    session.clear()
    g.user = None


def _redirect_to_login():
    """`next` is only ever safe to carry for a GET request - it's later
    followed with a plain GET once login succeeds (see app.py's /login), so
    threading it through for a POST (e.g. a stale /api/upload call) would
    redirect the browser to GET that same POST-only URL after logging in and
    405. Non-GET requests just land on /login with no `next`; by then the
    session is normally still valid anyway (see login_user's session.permanent)."""
    if request.method == "GET":
        return redirect(f"/login?next={request.path}")
    return redirect("/login")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _redirect_to_login()
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return _redirect_to_login()
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def max_login_attempts():
    return LOGIN_MAX_ATTEMPTS
