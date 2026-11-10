#!/usr/bin/env python3
"""
lifesheet.py — Dobby's life-tracking collector (replaces the FxLifeSheet Telegram worker).

Dobby calls this via `exec` to drive surveys over Discord, one question at a time,
and write answers into the shared Postgres `raw_data` table (source='dobby').

Commands:
  list                                  List surveys (schedule + question count)
  survey  <name>                        Print ordered questions for a survey (JSON)
  next    <name>                        Next unanswered question for today (daily/weekly), or {"done":true}
  today   <name>                        Keys already logged today for this survey
  insert  --key K --value V [--type T] [--question Q] [--allow-dup]
                                        Insert one answer into raw_data
  complete <name>                       Mark survey done now (updates last_run)

Survey defs come from lifesheet.json (next to this script). DB creds from .lifesheet.env
(DATABASE_URL=...) or the DATABASE_URL env var.
"""
import sys, os, json, time, argparse, datetime
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), ".pylibs"))
import pg8000.native  # vendored in ../.pylibs

from zoneinfo import ZoneInfo
IST = ZoneInfo(os.environ.get("LIFESHEET_TZ", "Asia/Kolkata"))  # travel-configurable timezone (was hardcoded IST). Name kept as IST for compatibility.
NUMERIC_TYPES = {"range", "boolean", "number"}
COMPLETABLE = ("awake", "asleep", "week", "workout")  # can be "done"; mood one-shot; whoop is auto-collected (excluded)
STATE_FILE = os.environ.get("LIFESHEET_STATE") or os.path.join(HERE, ".lifesheet-state.json")
REMINDER_THRESHOLD_MS = 8 * 60 * 60 * 1000  # re-ask an unanswered question after 8h (matches Telegram's nag throttle)
# Telegram scheduler.js thresholds: re-prompt a survey once it's overdue by this many hours since last run.
SCHEDULE_THRESHOLD_H = {"eightTimesADay": 24 / 8, "daily": 24 * 0.95,
                        "weekly": 24 * 7 * 1.05, "monthly": 24 * 30 * 1.05}


def out(obj):
    print(json.dumps(obj, default=str))


def database_url():
    url = os.environ.get("DATABASE_URL")
    envf = os.path.join(HERE, ".lifesheet.env")
    if not url and os.path.exists(envf):
        for line in open(envf):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not url:
        sys.exit("DATABASE_URL not configured (.lifesheet.env or env var)")
    return url


def connect():
    u = urlparse(database_url())
    db = (u.path or "").lstrip("/") or u.username
    return pg8000.native.Connection(
        user=u.username, password=u.password,
        host=u.hostname, port=u.port or 5432, database=db,
    )


def load_surveys():
    """Load survey definitions. Prod source is LIFESHEET_JSON_URL (Dropbox), cached
    locally and refreshed at most hourly; falls back to the last good cache offline."""
    cache = os.environ.get("LIFESHEET_JSON") or os.path.join(HERE, "lifesheet.json")
    url = os.environ.get("LIFESHEET_JSON_URL")
    if url:
        try:
            stale = (not os.path.exists(cache)) or (time.time() - os.path.getmtime(cache) > 3600)
            if stale:
                import urllib.request
                data = urllib.request.urlopen(url, timeout=15).read()
                json.loads(data)  # validate before persisting
                tmp = cache + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, cache)
        except Exception:
            pass  # keep using the last good cache
    with open(cache) as f:
        return json.load(f)


def find_question(surveys, key):
    for cmd, v in surveys.items():
        for q in v.get("questions", []):
            if q.get("key") == key:
                return cmd, q
    return None, None


def date_components(dt):
    iso = dt.isocalendar()
    return {
        "timestamp": int(dt.timestamp()) * 1000,  # whole-second ms (matrix-viz calendar bug: setHours(0,0,0) ignores ms)
        "yearmonth": dt.year * 100 + dt.month,
        "yearweek": dt.year * 100 + iso[1],
        "year": dt.year,
        "quarter": (dt.month - 1) // 3 + 1,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
        "week": iso[1],
        "matcheddate": dt.date(),
    }


def coerce_value(qtype, raw):
    """Validate/normalise an answer. Returns (value_str, error_or_None)."""
    raw = str(raw).strip()
    if qtype == "boolean":
        v = raw.lower()
        if v in ("1", "yes", "y", "true"):
            return "1", None
        if v in ("0", "no", "n", "false"):
            return "0", None
        return None, "boolean expects yes/no (1/0)"
    if qtype == "range":
        try:
            n = int(float(raw))
        except ValueError:
            return None, "range expects a number 0-6"
        if not (0 <= n <= 6):
            return None, "range value must be 0-6"
        return str(n), None
    if qtype == "number":
        try:
            float(raw)
        except ValueError:
            return None, "number expects a numeric value"
        return raw, None
    # text, location/city, header -> free text
    return raw, None


def cmd_list(args):
    s = load_surveys()
    out({k: {"schedule": v.get("schedule"),
             "questions": len([q for q in v.get("questions", []) if q.get("type") != "header"])}
         for k, v in s.items()})


def cmd_survey(args):
    s = load_surveys()
    if args.name not in s:
        sys.exit(f"unknown survey '{args.name}' (have: {', '.join(s)})")
    out({"survey": args.name, "schedule": s[args.name].get("schedule"),
         "questions": s[args.name].get("questions", [])})


def _logged_today(conn, survey, surveys):
    return set(_answers_today(conn, survey, surveys))


def _answers_today(conn, survey, surveys):
    keys = [q["key"] for q in surveys[survey].get("questions", []) if q.get("key")]
    if not keys:
        return {}
    today = datetime.datetime.now(IST).date()
    rows = conn.run(
        "SELECT key, value FROM raw_data WHERE source='dobby' AND matcheddate=:d "
        "AND key = ANY(:ks) ORDER BY importedat",
        d=today, ks=list(keys))
    return {key: value for key, value in rows}


def cmd_today(args):
    s = load_surveys()
    if args.name not in s:
        sys.exit(f"unknown survey '{args.name}'")
    conn = connect()
    try:
        out({"survey": args.name, "logged_today": sorted(_logged_today(conn, args.name, s))})
    finally:
        conn.close()


def cmd_next(args):
    s = load_surveys()
    if args.name not in s:
        sys.exit(f"unknown survey '{args.name}'")
    conn = connect()
    try:
        answers = _answers_today(conn, args.name, s)
        q = _next_q(args.name, s, set(answers), answers)
        if q:
            out({"done": False, "question": q})
            return
        out({"done": True})
    finally:
        conn.close()


def cmd_insert(args):
    surveys = load_surveys()
    cmd, q = find_question(surveys, args.key)
    qtype = args.type or (q.get("type") if q else None) or "text"
    question = args.question or (q.get("question") if q else None) or args.key
    value, err = coerce_value(qtype, args.value)
    if err:
        out({"inserted": False, "error": err, "key": args.key})
        sys.exit(2)

    dt = datetime.datetime.now(IST)
    c = date_components(dt)
    conn = connect()
    try:
        is_mood = cmd and surveys[cmd].get("schedule") == "eightTimesADay"
        if not args.allow_dup and not is_mood:
            dup = conn.run(
                "SELECT count(*) FROM raw_data WHERE source='dobby' AND key=:k AND matcheddate=:d",
                k=args.key, d=c["matcheddate"])
            if dup[0][0] > 0:
                out({"inserted": False, "skipped": "duplicate", "key": args.key,
                     "matcheddate": c["matcheddate"]})
                return
        conn.run(
            """INSERT INTO raw_data
               (timestamp, yearmonth, yearweek, year, quarter, month, day, hour, minute,
                week, key, question, type, value, matcheddate, source, importedat, importid)
               VALUES (:ts,:ym,:yw,:yr,:q,:mo,:d,:h,:mi,:wk,:k,:qn,:t,:v,:md,'dobby',:ia,NULL)""",
            ts=c["timestamp"], ym=c["yearmonth"], yw=c["yearweek"], yr=c["year"], q=c["quarter"],
            mo=c["month"], d=c["day"], h=c["hour"], mi=c["minute"], wk=c["week"],
            k=args.key, qn=question, t=qtype, v=value, md=c["matcheddate"], ia=dt)
        out({"inserted": True, "key": args.key, "type": qtype, "value": value,
             "matcheddate": c["matcheddate"], "survey": cmd})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queue + reminder state machine (replaces Telegram's queue + nag scheduler)
# State lives in .lifesheet-state.json and resets each IST day.
# ---------------------------------------------------------------------------
def _now_ms():
    return int(datetime.datetime.now(IST).timestamp() * 1000)


def _today_str():
    return datetime.datetime.now(IST).date().isoformat()


def load_state():
    fresh = {"date": _today_str(), "queue": [], "active": None,
             "last_prompt_at": 0, "last_prompt_key": None,
             "mood_active": False, "mood_asked": [], "skipped": []}
    if os.path.exists(STATE_FILE):
        try:
            cur = json.load(open(STATE_FILE))
            if cur.get("date") == fresh["date"]:
                return cur
        except Exception:
            pass
    return fresh


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _is_complete(conn, survey, surveys, extra_done=()):
    answers = _answers_today(conn, survey, surveys)
    done = set(answers) | set(extra_done)
    return _next_q(survey, surveys, done, answers) is None


def _condition_matches(q, answers):
    condition = q.get("showIf")
    if not condition:
        return True
    actual = answers.get(condition.get("key"))
    if "in" in condition:
        return str(actual) in {str(value) for value in condition["in"]}
    return str(actual) == str(condition.get("equals"))


def _next_q(survey, surveys, done, answers=None):
    answers = answers or {}
    for q in surveys[survey].get("questions", []):
        if q.get("type") == "header" or q.get("prompt") is False:
            continue
        if not _condition_matches(q, answers):
            continue
        if q.get("key") and q["key"] not in done:
            return q
    return None


def _count_remaining(survey, surveys, done, answers=None):
    """Answerable (non-header, not-yet-done) questions left in a survey."""
    answers = answers or {}
    return sum(1 for q in surveys.get(survey, {}).get("questions", [])
               if q.get("type") != "header" and q.get("prompt") is not False
               and q.get("key") and q["key"] not in done
               and _condition_matches(q, answers))


def _format_q(q, reminder=False, remaining=None):
    head = q["question"]
    if remaining is not None:                      # Telegram-style "N more / last question"
        head += " (last question)" if remaining <= 0 else \
                f" ({remaining} more question{'s' if remaining != 1 else ''})"
    lines = [head]
    btns = q.get("buttons")
    if btns:
        for k in sorted(btns, reverse=True):
            lines.append(f"{k} — {btns[k]}")
    elif q.get("type") == "boolean":
        lines.append("(yes / no)")
    elif q.get("type") == "range":
        lines.append("(0–6)")
    txt = "\n".join(lines)
    return ("⏰ Still need this one —\n" + txt) if reminder else txt


def _question_for_key(surveys, survey, key):
    for q in surveys.get(survey, {}).get("questions", []):
        if q.get("key") == key:
            return q
    return None


def _last_answer_ms(conn, survey, surveys):
    keys = [q["key"] for q in surveys[survey].get("questions", []) if q.get("key")]
    if not keys:
        return 0
    today = datetime.datetime.now(IST).date()
    rows = conn.run("SELECT max(importedat) FROM raw_data WHERE source='dobby' "
                    "AND matcheddate=:d AND key = ANY(:ks)", d=today, ks=list(keys))
    v = rows[0][0]
    if not v:
        return 0
    try:
        if v.tzinfo is None:
            v = v.replace(tzinfo=IST)
        return int(v.timestamp() * 1000)
    except Exception:
        return 0


def _set_last_run(conn, survey):
    conn.run("INSERT INTO last_run (command,last_run) VALUES (:c,:t) "
             "ON CONFLICT (command) DO UPDATE SET last_run=:t", c=survey, t=str(_now_ms()))


def _mark_done(conn, st, survey):
    _set_last_run(conn, survey)
    if survey in st["queue"]:
        st["queue"].remove(survey)
    if st["active"] == survey:
        st["active"] = None
        st["last_prompt_key"] = None


def cmd_enqueue(args):
    surveys = load_surveys()
    if args.name not in surveys:
        sys.exit(f"unknown survey '{args.name}'")
    st = load_state()
    if args.name in COMPLETABLE:
        conn = connect()
        try:
            _set_last_run(conn, args.name)  # Telegram-style: stamp run-time on trigger (relative schedule)
            if not _is_complete(conn, args.name, surveys, st.get("skipped", [])) \
                    and args.name != st["active"] and args.name not in st["queue"]:
                st["queue"].append(args.name)
        finally:
            conn.close()
    save_state(st)
    out({"enqueued": args.name, "queue": st["queue"], "active": st["active"]})


def _insert_row(conn, key, question, qtype, value):
    dt = datetime.datetime.now(IST)
    c = date_components(dt)
    conn.run(
        """INSERT INTO raw_data
           (timestamp, yearmonth, yearweek, year, quarter, month, day, hour, minute,
            week, key, question, type, value, matcheddate, source, importedat, importid)
           VALUES (:ts,:ym,:yw,:yr,:q,:mo,:d,:h,:mi,:wk,:k,:qn,:t,:v,:md,'dobby',:ia,NULL)""",
        ts=c["timestamp"], ym=c["yearmonth"], yw=c["yearweek"], yr=c["year"], q=c["quarter"],
        mo=c["month"], d=c["day"], h=c["hour"], mi=c["minute"], wk=c["week"],
        k=key, qn=question, t=qtype, v=value, md=c["matcheddate"], ia=dt)
    return c["matcheddate"]


def _compute_tick(mood=False):
    surveys = load_surveys()
    st = load_state()
    conn = connect()
    try:
        # 1. retire the active survey if it's finished
        if st["active"] and _is_complete(conn, st["active"], surveys, st.get("skipped", [])):
            _mark_done(conn, st, st["active"])
        # 2. promote the next queued survey
        if not st["active"] and st["queue"]:
            st["active"] = st["queue"].pop(0)
            st["last_prompt_key"] = None  # nothing asked yet for this survey
        # 3. ask / remind the active survey
        if st["active"]:
            answers = _answers_today(conn, st["active"], surveys)
            done = set(answers) | set(st.get("skipped", []))
            q = _next_q(st["active"], surveys, done, answers)
            if q is None:
                _mark_done(conn, st, st["active"])
            else:
                now = _now_ms()
                rem = _count_remaining(st["active"], surveys, done, answers) - 1  # excluding current question
                if st.get("last_prompt_key") != q["key"]:
                    # a question we haven't asked yet -> ask immediately (advance)
                    st["last_prompt_at"] = now
                    st["last_prompt_key"] = q["key"]
                    save_state(st)
                    return {"action": "ask", "survey": st["active"], "reminder": False,
                            "question": q, "post": _format_q(q, False, rem), "queue": st["queue"]}
                # same question still unanswered -> reminder, throttled
                if (now - st["last_prompt_at"]) >= REMINDER_THRESHOLD_MS:
                    st["last_prompt_at"] = now
                    save_state(st)
                    return {"action": "ask", "survey": st["active"], "reminder": True,
                            "question": q, "post": _format_q(q, True, rem), "queue": st["queue"]}
                save_state(st)
                return {"action": "wait", "survey": st["active"],
                        "minutes_since_activity": int((now - st["last_prompt_at"]) / 60000)}
        # 4. no completable survey active/queued -> mood session (one-at-a-time, fresh each slot)
        if not st.get("mood_active") and mood:
            st["mood_active"] = True
            st["mood_asked"] = []
            _set_last_run(conn, "mood")  # stamp run-time when a mood session starts
        if st.get("mood_active"):
            nextm = None
            for q in surveys.get("mood", {}).get("questions", []):
                if q.get("type") == "header":
                    continue
                if q.get("key") and q["key"] not in st["mood_asked"] \
                        and q["key"] not in st["skipped"]:
                    nextm = q
                    break
            if nextm is None:
                st["mood_active"] = False
                st["mood_asked"] = []
                st["last_prompt_key"] = None
                save_state(st)
                return {"action": "idle"}
            now = _now_ms()
            mood_done = set(st["mood_asked"]) | set(st.get("skipped", []))
            rem = _count_remaining("mood", surveys, mood_done) - 1  # excluding current
            if st.get("last_prompt_key") != nextm["key"]:
                # a mood question we haven't asked yet -> ask immediately (advance)
                st["last_prompt_at"] = now
                st["last_prompt_key"] = nextm["key"]
                save_state(st)
                return {"action": "ask", "survey": "mood", "reminder": False,
                        "question": nextm, "post": _format_q(nextm, False, rem), "queue": st["queue"]}
            # same mood question still unanswered -> re-ask only after the 8h throttle (no hourly spam)
            if (now - st["last_prompt_at"]) >= REMINDER_THRESHOLD_MS:
                st["last_prompt_at"] = now
                save_state(st)
                return {"action": "ask", "survey": "mood", "reminder": True,
                        "question": nextm, "post": _format_q(nextm, True, rem), "queue": st["queue"]}
            save_state(st)
            return {"action": "wait", "survey": "mood",
                    "minutes_since_activity": int((now - st["last_prompt_at"]) / 60000)}
        save_state(st)
        return {"action": "idle"}
    finally:
        conn.close()


def cmd_tick(args):
    out(_compute_tick(mood=args.mood))


def cmd_current(args):
    """Return the question currently awaiting an answer (from state), for reply context."""
    surveys = load_surveys()
    st = load_state()
    if not st.get("active") or not st.get("last_prompt_key"):
        out({"pending": False})
        return
    q = _question_for_key(surveys, st["active"], st["last_prompt_key"])
    if not q:
        out({"pending": False})
        return
    out({"pending": True, "survey": st["active"], "key": st["last_prompt_key"],
         "question": q, "post": _format_q(q)})


def cmd_answer(args):
    """Record Sam's reply to the currently-pending question (per state), then advance.
    The agent calls this on ANY answer in #lifesheet — context comes from the state
    file, never the chat history. Returns the next action ({action:ask, post:...} etc.)."""
    surveys = load_surveys()
    st = load_state()
    survey = st.get("active") or ("mood" if st.get("mood_active") else None)
    key = st.get("last_prompt_key")
    if not survey or not key:
        out({"ok": False, "error": "no_pending_question", "next": _compute_tick()})
        return
    q = _question_for_key(surveys, survey, key)
    qtype = (q or {}).get("type", "text")
    value, err = coerce_value(qtype, args.value)
    if err:
        out({"ok": False, "error": err, "key": key,
             "post": f"Hmm, {err}. {_format_q(q)}"})
        return
    is_mood = survey == "mood"
    conn = connect()
    try:
        dup = 0
        if not is_mood:
            today = datetime.datetime.now(IST).date()
            dup = conn.run("SELECT count(*) FROM raw_data WHERE source='dobby' AND key=:k "
                           "AND matcheddate=:d", k=key, d=today)[0][0]
        if dup == 0:
            _insert_row(conn, key, (q or {}).get("question", key), qtype, value)
    finally:
        conn.close()
    if is_mood:
        st = load_state()
        if key not in st["mood_asked"]:
            st["mood_asked"].append(key)
        save_state(st)
        mood_keys = [qq["key"] for qq in surveys.get("mood", {}).get("questions", []) if qq.get("key")]
        mdone = set(st["mood_asked"]) | set(st.get("skipped", []))
        done_survey = "mood" if all(k in mdone for k in mood_keys) else None
    else:
        conn2 = connect()
        try:
            done_survey = survey if _is_complete(conn2, survey, surveys,
                                                 load_state().get("skipped", [])) else None
        finally:
            conn2.close()
    nxt = _compute_tick(mood=is_mood)
    out({"ok": True, "saved": {"key": key, "value": value},
         "done_survey": done_survey, "next": nxt})


def cmd_skip(args):
    """Skip the currently-pending question (don't record it) and advance."""
    st = load_state()
    survey = st.get("active") or ("mood" if st.get("mood_active") else None)
    key = st.get("last_prompt_key")
    if not survey or not key:
        out({"skipped": None, "next": _compute_tick()})
        return
    is_mood = survey == "mood"
    if is_mood:
        if key not in st["mood_asked"]:
            st["mood_asked"].append(key)
    elif key not in st["skipped"]:
        st["skipped"].append(key)
    save_state(st)
    out({"skipped": key, "next": _compute_tick(mood=is_mood)})


def cmd_due(args):
    """Which surveys are overdue per their schedule type (Telegram relative model).
    Returns {"overdue": [completable...], "mood_due": bool}."""
    surveys = load_surveys()
    conn = connect()
    try:
        rows = {}
        for r in conn.run("SELECT command, last_run FROM last_run"):
            try:
                rows[r[0]] = int(r[1])
            except (TypeError, ValueError):
                pass
        now = _now_ms()
        overdue, mood_due = [], False
        for name, v in surveys.items():
            thr_h = SCHEDULE_THRESHOLD_H.get(v.get("schedule"))
            if thr_h is None:                      # manual (whoop) / unknown -> never auto-prompt
                continue
            if name != "mood" and name not in COMPLETABLE:
                continue
            if (now - rows.get(name, 0)) >= thr_h * 3600 * 1000:
                if name == "mood":
                    mood_due = True
                else:
                    overdue.append(name)
        out({"overdue": overdue, "mood_due": mood_due})
    finally:
        conn.close()


def cmd_complete(args):
    s = load_surveys()
    if args.name not in s:
        sys.exit(f"unknown survey '{args.name}'")
    now_ms = int(datetime.datetime.now(IST).timestamp() * 1000)
    conn = connect()
    try:
        conn.run(
            """INSERT INTO last_run (command, last_run) VALUES (:c, :t)
               ON CONFLICT (command) DO UPDATE SET last_run = :t""",
            c=args.name, t=str(now_ms))
        out({"completed": args.name, "last_run_ms": now_ms})
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(prog="lifesheet")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    for name, fn in (("survey", cmd_survey), ("next", cmd_next),
                     ("today", cmd_today), ("complete", cmd_complete)):
        sp = sub.add_parser(name)
        sp.add_argument("name")
        sp.set_defaults(fn=fn)
    ip = sub.add_parser("insert")
    ip.add_argument("--key", required=True)
    ip.add_argument("--value", required=True)
    ip.add_argument("--type")
    ip.add_argument("--question")
    ip.add_argument("--allow-dup", action="store_true")
    ip.set_defaults(fn=cmd_insert)
    ep = sub.add_parser("enqueue")
    ep.add_argument("name")
    ep.set_defaults(fn=cmd_enqueue)
    tp = sub.add_parser("tick")
    tp.add_argument("--mood", action="store_true")
    tp.set_defaults(fn=cmd_tick)
    sub.add_parser("current").set_defaults(fn=cmd_current)
    sub.add_parser("skip").set_defaults(fn=cmd_skip)
    sub.add_parser("due").set_defaults(fn=cmd_due)
    ap = sub.add_parser("answer")
    ap.add_argument("value")
    ap.set_defaults(fn=cmd_answer)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
