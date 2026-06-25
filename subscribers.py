#!/usr/bin/env python3
"""Subscriber store for the daily podcast, backed by your Supabase project.

Reads/writes via the Supabase Management API (database/query) using the single
account token SUPABASE_ACCESS_TOKEN (from .env). One
secret for everything: setup, the daily read, and CLI management. No Postgres
driver, no venv -- the daily run stays dependency-free.

Why curl, not urllib (scar 2026-06-22): Cloudflare in front of api.supabase.com
blocks Python-urllib's signature with HTTP 403 code 1010. curl (always present on
macOS, on launchd's PATH at /usr/bin/curl) is not blocked. So requests shell out
to curl, with the token passed via a 0600 temp config file (-K), never on argv
(so it can't leak through `ps`).

Project ref: SUPABASE_PROJECT_REF env override, else supabase.json next to this
file (non-secret).

Commands:
  list           active subscriber emails, one per line. Exit 3 if the token is
                 missing; exit 4 on API error.
  list-all       "email<TAB>status" for every row.
  add --email E [--name N] [--source S]   upsert a subscriber (active).
  unsubscribe --email E                    mark unsubscribed (keeps the row).
  selftest       offline checks (no network).
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile

API = "https://api.supabase.com/v1/projects/{ref}/database/query"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIMEOUT = 45


class CredsMissing(Exception):
    pass


class ApiError(Exception):
    pass


def _token():
    tok = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    if not tok:
        raise CredsMissing("SUPABASE_ACCESS_TOKEN not set")
    return tok


def _ref():
    ref = (os.environ.get("SUPABASE_PROJECT_REF") or "").strip()
    if ref:
        return ref
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            return json.load(fh)["project_ref"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise CredsMissing(f"no project ref (SUPABASE_PROJECT_REF or {cfg}): {exc}")


def sql_str(value):
    """Single-quote-escape a string for inline SQL."""
    return "'" + str(value).replace("'", "''") + "'"


def valid_email(email):
    return bool(EMAIL_RE.match((email or "").strip()))


def run_query(sql):
    """POST one SQL statement to the Management API via curl. Returns parsed JSON
    (a list of row dicts, or [] for no rows)."""
    ref, tok = _ref(), _token()
    fd, cfg = tempfile.mkstemp(prefix="sb-curl-", suffix=".cfg")
    try:
        os.write(fd, f'header = "Authorization: Bearer {tok}"\n'.encode())
        os.write(fd, b'header = "Content-Type: application/json"\n')
        os.close(fd)
        proc = subprocess.run(
            ["curl", "-s", "-K", cfg, "-X", "POST", API.format(ref=ref),
             "--data", "@-"],
            input=json.dumps({"query": sql}),
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    finally:
        try:
            os.unlink(cfg)
        except OSError:
            pass
    if proc.returncode != 0:
        raise ApiError(f"curl failed ({proc.returncode}): {proc.stderr[:200]}")
    out = proc.stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict) and data.get("message"):
        raise ApiError(data["message"])
    return data


def active_emails():
    rows = run_query(
        "select email from subscribers where status = 'active' "
        "order by created_at asc"
    )
    return [r["email"] for r in rows if r.get("email")]


def _guarded(fn):
    """Run a command, mapping creds/API errors to exit codes 3/4."""
    try:
        return fn()
    except CredsMissing as exc:
        print(f"subscribers: {exc}", file=sys.stderr)
        return 3
    except ApiError as exc:
        print(f"subscribers: {exc}", file=sys.stderr)
        return 4


def cmd_list(_args):
    def go():
        for email in active_emails():
            print(email)
        return 0
    return _guarded(go)


def cmd_list_all(_args):
    def go():
        rows = run_query("select email, status from subscribers order by created_at asc")
        for r in rows:
            print(f"{r.get('email','')}\t{r.get('status','')}")
        return 0
    return _guarded(go)


def cmd_add(args):
    email = args.email.strip().lower()
    if not valid_email(email):
        print(f"subscribers: invalid email: {args.email}", file=sys.stderr)
        return 2

    def go():
        name = sql_str(args.name) if args.name else "null"
        source = sql_str(args.source or "manual")
        run_query(
            f"insert into subscribers (email, name, source, status) "
            f"values ({sql_str(email)}, {name}, {source}, 'active') "
            f"on conflict (email) do update set status = 'active', "
            f"unsubscribed_at = null"
        )
        print(f"added/reactivated {email}")
        return 0
    return _guarded(go)


def cmd_unsubscribe(args):
    email = args.email.strip().lower()
    if not valid_email(email):
        print(f"subscribers: invalid email: {args.email}", file=sys.stderr)
        return 2

    def go():
        run_query(
            f"update subscribers set status = 'unsubscribed', "
            f"unsubscribed_at = now() where email = {sql_str(email)}"
        )
        print(f"unsubscribed {email}")
        return 0
    return _guarded(go)


def cmd_promote(args):
    """Activate the N oldest held (pending) subscribers -> they get the next send.
    The warmup lever: promote a batch, watch unsubscribes, promote more."""
    n = int(args.count)

    def go():
        rows = run_query(
            "update subscribers set status = 'active' where id in "
            f"(select id from subscribers where status = 'pending' "
            f"order by created_at asc limit {n}) returning email"
        )
        print(f"promoted {len(rows)} pending -> active")
        for r in rows:
            print(f"  + {r.get('email')}")
        return 0
    return _guarded(go)


def cmd_stats(_args):
    """Counts by status -- the unsubscribe watch."""
    def go():
        rows = run_query("select status, count(*) as c from subscribers group by status order by status")
        for r in rows:
            print(f"{r.get('status')}\t{r.get('c')}")
        return 0
    return _guarded(go)


def cmd_selftest(_args):
    """Offline reproducer: SQL escaping blocks quote injection, email validation
    rejects junk, ref resolves from supabase.json, missing token raises."""
    ok = True

    if sql_str("o'brien@x.com") != "'o''brien@x.com'":
        print("FAIL: SQL escaping wrong", file=sys.stderr); ok = False
    if valid_email("not-an-email") or not valid_email("a@b.co"):
        print("FAIL: email validation wrong", file=sys.stderr); ok = False

    os.environ["SUPABASE_PROJECT_REF"] = "selftest-ref"
    try:
        if _ref() != "selftest-ref":
            print("FAIL: ref did not resolve from env", file=sys.stderr); ok = False
    except CredsMissing as exc:
        print(f"FAIL: ref unresolved: {exc}", file=sys.stderr); ok = False
    finally:
        os.environ.pop("SUPABASE_PROJECT_REF", None)

    saved = os.environ.pop("SUPABASE_ACCESS_TOKEN", None)
    try:
        _token()
        print("FAIL: missing token did not raise", file=sys.stderr); ok = False
    except CredsMissing:
        pass
    finally:
        if saved is not None:
            os.environ["SUPABASE_ACCESS_TOKEN"] = saved

    # negative self-test: cmd_list returns 3 (fallback), not 0, with no token.
    s2 = os.environ.pop("SUPABASE_ACCESS_TOKEN", None)
    try:
        if cmd_list(None) != 3:
            print("FAIL: cmd_list w/o token should return 3", file=sys.stderr); ok = False
    finally:
        if s2 is not None:
            os.environ["SUPABASE_ACCESS_TOKEN"] = s2

    print("PASS" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="podcast subscriber store (Supabase)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("list-all").set_defaults(func=cmd_list_all)

    add = sub.add_parser("add")
    add.add_argument("--email", required=True)
    add.add_argument("--name", default=None)
    add.add_argument("--source", default=None)
    add.set_defaults(func=cmd_add)

    uns = sub.add_parser("unsubscribe")
    uns.add_argument("--email", required=True)
    uns.set_defaults(func=cmd_unsubscribe)

    pr = sub.add_parser("promote")
    pr.add_argument("--count", type=int, required=True)
    pr.set_defaults(func=cmd_promote)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    sub.add_parser("selftest").set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
