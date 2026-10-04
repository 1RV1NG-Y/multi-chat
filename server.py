#!/usr/bin/env python3
"""multi-chat — a group chat between you, Claude Code and Codex.

Each chat keeps one persistent headless session per agent (`claude -p --resume`,
`codex exec resume`). The server tracks which messages each agent has already been
shown and, on every turn, sends it only what's new, labelled by author.

Standard library only:  python3 server.py [--port 8765] [--open]
"""
import argparse
import glob
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
AGENTS = ("claude", "codex")
LABEL = {"user": "User", "claude": "Claude", "codex": "Codex"}
OTHER = {"claude": "codex", "codex": "claude"}

FRAMING = """You are {me}, in a three-way design discussion with a human (the User) and another AI assistant ({other}), \
run through a small app that relays messages between you. The User mostly brings ideas, designs and trade-offs to \
think through, but follow their lead on whatever they ask.

Messages arrive labelled by author, e.g. "[User]: ...". Reply only as yourself, in plain prose (markdown is fine). \
Don't prefix your reply with your own label, and don't write lines for the other participants.

Be a useful collaborator, not an agreeable one: say plainly when you disagree and why, point out what's missing, \
and keep replies focused."""

VISIBILITY = {
    "manual": "How this chat shares messages: you receive every message from the User, including ones addressed "
              "only to {other} (marked \"[User → {other}]\"), but NOT {other}'s replies. That's deliberate, so the "
              "two of you give independent opinions; it isn't a bug. You'll receive {other}'s replies only when the "
              "User forwards one to you, starts a debate, or asks for a synthesis, and they'll be quoted explicitly.",
    "auto": "How this chat shares messages: group-chat mode. Each turn you receive everything said since your last "
            "turn, including {other}'s replies, labelled \"[{other}]\".",
}

DEFAULT_SETTINGS = {
    # manual: agents see each other's replies only when you forward / debate / synthesize
    # auto:   true group chat — every turn, each agent catches up on everything it hasn't seen
    "share": "manual",
    # none | read | write  (what the CLIs may do in the chat's working directory)
    "tools": "read",
    "agents": {a: {"model": "", "effort": "", "persona": ""} for a in AGENTS},
}


def now():
    return time.time()


def new_id():
    return uuid.uuid4().hex[:12]


def find_codex():
    if os.environ.get("CODEX_BIN"):
        return os.environ["CODEX_BIN"]
    path = shutil.which("codex")
    if path and runs(path):
        return path
    # The npm package's `codex` is a node wrapper around a native binary. If node itself
    # is broken (e.g. a partial system upgrade), run the native binary directly.
    roots = ["/usr/lib/node_modules", "/usr/local/lib/node_modules",
             os.path.expanduser("~/.npm-global/lib/node_modules"),
             os.path.expanduser("~/.local/lib/node_modules")]
    for r in roots:
        for hit in sorted(glob.glob(f"{r}/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex")):
            if os.access(hit, os.X_OK) and runs(hit):
                return hit
    return path or "codex"


def runs(binary):
    try:
        return subprocess.run([binary, "--version"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def version(binary):
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except Exception:
        return None


CLAUDE_MODELS = [
    ("opus", "Opus (latest)", "Alias for the newest Opus"),
    ("sonnet", "Sonnet (latest)", "Alias for the newest Sonnet"),
    ("haiku", "Haiku (latest)", "Alias for the newest Haiku"),
    ("claude-fable-5-1", "Fable 5.1", ""),
    ("claude-opus-5-5", "Opus 5.5", ""),
    ("claude-sonnet-5-5", "Sonnet 5.5", ""),
    ("claude-haiku-4-5-20251001", "Haiku 4.5", ""),
]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def read_json(path):
    try:
        return json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return {}


def models():
    """Model choices for the settings dropdowns, plus what each CLI uses when left on default."""
    claude_settings = read_json("~/.claude/settings.json")
    claude = {
        "default": {"model": claude_settings.get("model", ""), "effort": ""},
        "models": [{"id": i, "name": n, "description": d, "efforts": CLAUDE_EFFORTS} for i, n, d in CLAUDE_MODELS],
    }
    # Codex caches the models available to your account; list the ones its own picker shows, minus
    # provider-prefixed plugin entries like "chatgpt-web/zero-risk", which hand the prompt to a browser
    # tab instead of answering.
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    cache = read_json(codex_home / "models_cache.json")
    try:
        import tomllib
        config = tomllib.loads((codex_home / "config.toml").read_text())
    except (OSError, ValueError):
        config = {}
    codex = {
        "default": {"model": config.get("model", ""), "effort": config.get("model_reasoning_effort", "")},
        "models": [{"id": m["slug"], "name": m.get("display_name") or m["slug"],
                    "description": m.get("description", ""),
                    "efforts": [lvl["effort"] for lvl in m.get("supported_reasoning_levels", [])],
                    "default_effort": m.get("default_reasoning_level", "")}
                   for m in sorted(cache.get("models", []), key=lambda m: m.get("priority", 99))
                   if m.get("slug") and "/" not in m["slug"] and m.get("visibility", "list") == "list"],
    }
    return {"claude": claude, "codex": codex}


def mentions(text):
    found = [a for a in AGENTS if re.search(rf"(^|\s)@{a}\b", text, re.I)]
    return found


# --------------------------------------------------------------------------- storage

class Store:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / "chats"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chats = {}
        for f in self.dir.glob("*.json"):
            try:
                chat = json.loads(f.read_text())
            except Exception as e:
                print(f"skipping unreadable chat {f}: {e}")
                continue
            for m in chat["messages"]:
                if m["status"] in ("pending", "streaming"):
                    m["status"] = "error"
                    m["error"] = "Interrupted (server restarted)."
            self.chats[chat["id"]] = chat

    def save(self, chat):
        chat["updated"] = now()
        tmp = self.dir / f".{chat['id']}.tmp"
        tmp.write_text(json.dumps(chat, indent=1))
        os.replace(tmp, self.dir / f"{chat['id']}.json")

    def delete(self, cid):
        self.chats.pop(cid, None)
        (self.dir / f"{cid}.json").unlink(missing_ok=True)


class Hub:
    """Fan-out of server-sent events per chat."""

    def __init__(self):
        self.subs = {}
        self.lock = threading.Lock()

    def subscribe(self, cid):
        q = queue.Queue()
        with self.lock:
            self.subs.setdefault(cid, set()).add(q)
        return q

    def unsubscribe(self, cid, q):
        with self.lock:
            self.subs.get(cid, set()).discard(q)

    def publish(self, cid, event):
        with self.lock:
            targets = list(self.subs.get(cid, ()))
        for q in targets:
            q.put(event)


# --------------------------------------------------------------------------- app

class App:
    def __init__(self, data_dir: Path):
        self.store = Store(data_dir)
        self.hub = Hub()
        self.lock = threading.RLock()          # guards all chat mutation
        self.agent_locks = {}                  # (cid, agent) -> Lock: one turn per session at a time
        self.status = {}                       # cid -> agent -> {state, activity}
        self.procs = {}                        # (cid, agent) -> Popen
        self.stops = {}                        # cid -> Event for the current generation of actions
        self.bins = {"claude": os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude",
                     "codex": find_codex()}
        self.versions = {a: version(b) for a, b in self.bins.items()}

    # ---- helpers

    def chat(self, cid):
        chat = self.store.chats.get(cid)
        if not chat:
            raise KeyError(cid)
        return chat

    def agent_lock(self, cid, agent):
        with self.lock:
            return self.agent_locks.setdefault((cid, agent), threading.Lock())

    def stop_event(self, cid):
        with self.lock:
            return self.stops.setdefault(cid, threading.Event())

    def set_status(self, cid, agent, state, activity=""):
        st = self.status.setdefault(cid, {}).setdefault(agent, {})
        st.update(state=state, activity=activity)
        self.hub.publish(cid, {"type": "status", "agent": agent, **st})

    def statuses(self, cid):
        st = self.status.get(cid, {})
        return {a: st.get(a, {"state": "idle", "activity": ""}) for a in AGENTS}

    def add_message(self, chat, author, text="", status="done", **extra):
        m = {"id": new_id(), "author": author, "text": text, "ts": now(), "status": status, "kind": "msg"}
        m.update(extra)
        chat["messages"].append(m)
        self.hub.publish(chat["id"], {"type": "message", "message": m})
        return m

    def push(self, chat, m, save=False):
        self.hub.publish(chat["id"], {"type": "message", "message": m})
        if save:
            self.store.save(chat)

    def find_message(self, chat, mid):
        for m in chat["messages"]:
            if m["id"] == mid:
                return m
        raise KeyError(mid)

    # ---- chat CRUD

    def list_chats(self):
        chats = sorted(self.store.chats.values(), key=lambda c: c["updated"], reverse=True)
        return [{"id": c["id"], "title": c["title"], "updated": c["updated"]} for c in chats]

    def new_chat(self, workdir=None):
        cid = new_id()
        if workdir:
            workdir = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir):
                raise ValueError(f"Not a directory: {workdir}")
        else:
            workdir = str(self.store.root / "workspaces" / cid)
            os.makedirs(workdir, exist_ok=True)
        chat = {
            "id": cid, "title": "New chat", "created": now(), "updated": now(), "workdir": workdir,
            "settings": json.loads(json.dumps(DEFAULT_SETTINGS)),
            "agents": {a: {"session": None, "delivered": [], "persona_sent": "", "mode_sent": None} for a in AGENTS},
            "messages": [],
            "prompts": {},
        }
        with self.lock:
            self.store.chats[cid] = chat
            self.store.save(chat)
        return chat

    def update_chat(self, cid, body):
        with self.lock:
            chat = self.chat(cid)
            if "title" in body:
                chat["title"] = str(body["title"]).strip()[:120] or chat["title"]
            if "workdir" in body and body["workdir"] != chat["workdir"]:
                if any(chat["agents"][a]["session"] for a in AGENTS):
                    raise ValueError("The working directory can't change once the agents have sessions.")
                wd = os.path.abspath(os.path.expanduser(body["workdir"]))
                if not os.path.isdir(wd):
                    raise ValueError(f"Not a directory: {wd}")
                chat["workdir"] = wd
            if "settings" in body:
                s, new = chat["settings"], body["settings"]
                if new.get("share") in ("manual", "auto"):
                    s["share"] = new["share"]
                if new.get("tools") in ("none", "read", "write"):
                    s["tools"] = new["tools"]
                for a in AGENTS:
                    for k in ("model", "effort", "persona"):
                        if k in new.get("agents", {}).get(a, {}):
                            s["agents"][a][k] = str(new["agents"][a][k]).strip()
            self.store.save(chat)
            self.hub.publish(cid, {"type": "chat", "chat": self.meta(chat)})
            return chat

    def meta(self, chat):
        return {k: chat[k] for k in ("id", "title", "workdir", "settings")} | {
            "agents": {a: {"session": chat["agents"][a]["session"], "delivered": chat["agents"][a]["delivered"]}
                       for a in AGENTS}}

    @staticmethod
    def public(chat):
        """The chat as sent to the browser: prompts are fetched one at a time, on demand."""
        return {k: v for k, v in chat.items() if k != "prompts"}

    def delete_chat(self, cid):
        self.stop(cid)
        with self.lock:
            self.store.delete(cid)

    # ---- actions

    def send(self, cid, text, to=None):
        text = text.strip()
        if not text:
            raise ValueError("Empty message.")
        targets = mentions(text) or [a for a in (to or AGENTS) if a in AGENTS]
        if not targets:
            raise ValueError("No recipients.")
        stop = self.stop_event(cid)
        with self.lock:
            chat = self.chat(cid)
            if chat["title"] == "New chat":
                chat["title"] = text.splitlines()[0][:60]
                self.hub.publish(cid, {"type": "chat", "chat": self.meta(chat)})
            self.add_message(chat, "user", text, to=targets)
            group = new_id() if len(targets) > 1 else None
            jobs = [(a, self.add_message(chat, a, status="pending", group=group)) for a in targets]
            self.store.save(chat)
        for agent, placeholder in jobs:
            self.spawn(cid, agent, placeholder, {"kind": "reply"}, stop)

    def forward(self, cid, mid, to, note=""):
        if to not in AGENTS:
            raise ValueError("Unknown agent.")
        stop = self.stop_event(cid)
        with self.lock:
            chat = self.chat(cid)
            src = self.find_message(chat, mid)
            if src["author"] == to:
                raise ValueError("That's the agent's own message.")
            fwd = self.add_message(chat, "user", note.strip(), kind="forward", of=mid, of_author=src["author"], to=[to])
            placeholder = self.add_message(chat, to, status="pending", reply_to=mid)
            self.store.save(chat)
        self.spawn(cid, to, placeholder, {"kind": "forward", "source": mid, "note": fwd["id"]}, stop)

    def synthesize(self, cid, by):
        if by not in AGENTS:
            raise ValueError("Unknown agent.")
        stop = self.stop_event(cid)
        with self.lock:
            chat = self.chat(cid)
            self.add_message(chat, "user", "", kind="synthesize", to=[by])
            placeholder = self.add_message(chat, by, status="pending", synthesis=True)
            self.store.save(chat)
        self.spawn(cid, by, placeholder, {"kind": "synthesize"}, stop)

    def debate(self, cid, rounds, first, topic=""):
        rounds = max(1, min(int(rounds), 10))
        if first not in AGENTS:
            raise ValueError("Unknown agent.")
        stop = self.stop_event(cid)
        with self.lock:
            chat = self.chat(cid)
            marker = self.add_message(chat, "user", (topic or "").strip(), kind="debate", rounds=rounds, first=first)
            self.store.save(chat)
        threading.Thread(target=self._debate, args=(cid, marker["id"], 0, None, stop), daemon=True).start()

    def _debate(self, cid, marker_id, start, placeholder, stop):
        """Run debate turns start..end. `placeholder` is an existing message to reuse for the first turn (retry)."""
        with self.lock:
            chat = self.store.chats.get(cid)
            if not chat:
                return
            marker = self.find_message(chat, marker_id)
        order = [marker["first"], OTHER[marker["first"]]]
        total = marker["rounds"] * 2
        for i in range(start, total):
            if stop.is_set():
                return
            speaker = order[i % 2]
            with self.lock:
                chat = self.store.chats.get(cid)
                if not chat:
                    return
                msgs = chat["messages"]
                after = msgs[msgs.index(marker) + 1:]
                replied = lambda m: m["author"] == OTHER[speaker] and m["status"] in ("done", "stopped") and m["text"]
                # Respond to the other's previous debate turn. The opening turn takes the topic if there is one,
                # otherwise the other's latest reply from before the debate.
                last = next((m for m in reversed(after) if replied(m) and m.get("debate")), None)
                if i == 0 and not marker["text"]:
                    last = next((m for m in reversed(msgs) if replied(m)), None)
                    if not last:
                        self.add_message(chat, "system", f"{LABEL[OTHER[speaker]]} hasn't said anything to respond "
                                                         "to yet. Give the debate a topic, or ask something first.")
                        self.store.save(chat)
                        return
                elif i > 0 and not last:
                    return
                action = {"kind": "debate", "source": last["id"] if last else None, "debate": marker_id,
                          "step": i + 1, "total": total, "retry": placeholder is not None}
                if placeholder is None:
                    placeholder = self.add_message(chat, speaker, status="pending",
                                                   reply_to=last["id"] if last else None, debate=f"{i + 1}/{total}")
                self.store.save(chat)
            ok = self.turn(cid, speaker, placeholder, action, stop)
            placeholder = None
            if not ok:
                return

    def retry(self, cid, mid):
        stop = self.stop_event(cid)
        with self.lock:
            chat = self.chat(cid)
            m = self.find_message(chat, mid)
            if m["author"] not in AGENTS or m["status"] not in ("error", "stopped"):
                raise ValueError("Only failed or stopped replies can be retried.")
            action = m.get("action")
            if not action:
                if m.get("debate") or m.get("reply_to") or m.get("synthesis"):
                    raise ValueError("This reply is from before retry was added, so it can't be retried.")
                action = {"kind": "reply"}
            for k in ("error", "warning", "finished"):
                m.pop(k, None)
            m.update(text="", status="pending", ts=now())
            self.push(chat, m, save=True)
        if action["kind"] == "debate":
            threading.Thread(target=self._debate, args=(cid, action["debate"], action["step"] - 1, m, stop),
                             daemon=True).start()
        else:
            self.spawn(cid, m["author"], m, action | {"retry": True}, stop)

    def stop(self, cid):
        with self.lock:
            ev = self.stops.pop(cid, None)
            procs = [p for (c, _), p in self.procs.items() if c == cid]
        if ev:
            ev.set()
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    # ---- turns

    def spawn(self, cid, agent, placeholder, action, stop):
        threading.Thread(target=self.turn, args=(cid, agent, placeholder, action, stop), daemon=True).start()

    def turn(self, cid, agent, msg, action, stop):
        lock = self.agent_lock(cid, agent)
        if lock.locked():
            self.set_status(cid, agent, "queued")
        with lock:
            with self.lock:
                chat = self.store.chats.get(cid)
                if not chat:
                    return False
                if stop.is_set():
                    msg.update(status="stopped")
                    self.push(chat, msg, save=True)
                    return False
                prompts = chat.setdefault("prompts", {})
                prev = prompts.get(msg["id"]) if action.get("retry") else None
                if prev:
                    # Resend what the failed attempt was sent; a partial run may already have marked it as seen.
                    original, delivered, sent = prev["original"], prev["delivered"], prev["sent"]
                    prompt = ("(Retry: your previous attempt at this reply was interrupted or failed, so here is "
                              "the same request again. Answer it in full.)\n\n" + original)
                else:
                    prompt, delivered, sent = self.build_prompt(chat, agent, action)
                    original = prompt
                cmd = self.command(chat, agent)
                prompts[msg["id"]] = {"prompt": prompt, "original": original, "delivered": delivered, "sent": sent,
                                      "command": [os.path.basename(cmd[0])] + cmd[1:], "ts": now()}
                msg.update(status="streaming", action={k: v for k, v in action.items() if k != "retry"})
                self.push(chat, msg)
            self.set_status(cid, agent, "running", "starting…")
            try:
                session, error, warnings = self.run_cli(chat, agent, cmd, prompt, msg, stop)
            except Exception as e:  # noqa: BLE001 — surface anything to the UI
                session, error, warnings = None, f"{type(e).__name__}: {e}", []
            with self.lock:
                st = chat["agents"][agent]
                if session:
                    st["session"] = session
                    st["delivered"].extend(i for i in delivered if i not in st["delivered"])
                    st.update(sent)
                if warnings:
                    msg["warning"] = "\n".join(warnings)
                if stop.is_set():
                    msg["status"] = "stopped"
                elif (error or warnings) and not msg["text"].strip():
                    msg.update(status="error", error=error or msg.pop("warning"))
                else:
                    msg["status"] = "done"
                    if error:
                        msg["error"] = error
                msg["finished"] = now()
                self.push(chat, msg, save=True)
                self.hub.publish(cid, {"type": "chat", "chat": self.meta(chat)})
            self.set_status(cid, agent, "idle")
            return msg["status"] == "done"

    def build_prompt(self, chat, agent, action):
        other = OTHER[agent]
        st, settings = chat["agents"][agent], chat["settings"]
        seen = set(st["delivered"])
        explicit = {action.get("source"), action.get("note"), action.get("debate")} - {None}
        # Debates and syntheses need the full picture, so they also catch up on the other's unseen replies.
        share_all = settings["share"] == "auto" or action["kind"] in ("synthesize", "debate")

        pending = []
        for m in chat["messages"]:
            if m["id"] in seen or m["id"] in explicit or m["author"] in (agent, "system"):
                continue
            if m["author"] == other and (not share_all or m["status"] not in ("done", "stopped") or not m["text"]):
                continue
            if m["author"] == "user" and m["kind"] != "msg" and not m["text"]:
                continue  # bare debate/synthesize markers carry no content
            pending.append(m)

        parts = []
        mode = settings["share"]
        visibility = VISIBILITY[mode].format(other=LABEL[other])
        if not st["session"]:
            parts.append(FRAMING.format(me=LABEL[agent], other=LABEL[other]) + "\n\n" + visibility)
        elif st.get("mode_sent") != mode:
            parts.append("Update from the app: " + visibility)
        persona = settings["agents"][agent]["persona"]
        if persona != st["persona_sent"]:
            parts.append(f"Your role in this discussion: {persona}" if persona
                         else "Your previously assigned role no longer applies; just be yourself.")
        if pending:
            parts.append("New in the conversation since your last turn:\n\n" + "\n\n".join(self.fmt(m) for m in pending))

        kind = action["kind"]
        if kind == "forward":
            src = self.find_message(chat, action["source"])
            note = self.find_message(chat, action["note"])["text"]
            parts.append(f"The User forwarded this message from {LABEL[src['author']]} to you:\n\n"
                         f"{self.quote(src)}\n\n"
                         + (f"The User's note: {note}\n\n" if note else "")
                         + "Give your honest take on it: what's strong, what's weak or missing, "
                           "and what you'd do differently.")
        elif kind == "debate":
            step, total = action["step"], action["total"]
            marker = self.find_message(chat, action["debate"])
            text = f"Debate between you and {LABEL[other]}, turn {step} of {total} (you alternate; {total // 2} each)."
            if marker["text"] and marker["id"] not in seen:
                text += f"\n\nThe User's topic for this debate:\n\n{marker['text']}"
            if action.get("source"):
                text += f"\n\n{LABEL[other]} said:\n\n{self.quote(self.find_message(chat, action['source']))}"
                text += (f"\n\nRespond directly to {LABEL[other]}: challenge what you disagree with, concede what's "
                         "right, and push toward the best answer. Keep it tight.")
            else:
                text += f"\n\nYou open the debate: give your position on the topic. Keep it tight."
            if step == total:
                text += (f"\n\nThis is the final turn of the debate. After responding, close with where you and "
                         f"{LABEL[other]} agree, where you still disagree, and what you'd recommend.")
            elif step == total - 1:
                text += f"\n\n{LABEL[other]} has the final turn after you, so don't write a closing summary yet."
            else:
                text += f"\n\n{total - step} more turns follow this one."
            parts.append(text)
        elif kind == "synthesize":
            parts.append(f"The User asks you to synthesize the whole discussion so far, including {LABEL[other]}'s "
                         "contributions: where you agree, where you still disagree (with the strongest argument on "
                         "each side), open questions, and a concrete recommended next step.")
        elif not pending:
            parts.append("(No new messages — continue the discussion.)")

        delivered = [m["id"] for m in pending] + sorted(explicit)
        return "\n\n---\n\n".join(parts), delivered, {"persona_sent": persona, "mode_sent": mode}

    @staticmethod
    def debate_label(m):
        if not m.get("debate"):
            return ""
        step, total = m["debate"].split("/")
        return f"debate turn {step} of {total}" + (", final" if step == total else "")

    def fmt(self, m):
        who = LABEL.get(m["author"], m["author"])
        if m["kind"] == "forward":
            to = ", ".join(LABEL[a] for a in m.get("to", []))
            return f"[User, forwarding {LABEL.get(m.get('of_author'), '?')}'s message to {to}]: {m['text'] or '(no note)'}"
        if m["kind"] == "debate":
            return f"[User, starting a debate on]: {m['text']}"
        if m["author"] == "user" and m.get("to") and len(m["to"]) < len(AGENTS):
            return f"[User → {LABEL[m['to'][0]]}]: {m['text']}"
        if m.get("debate"):
            return f"[{who}, {self.debate_label(m)}]: {m['text']}"
        return f"[{who}]: {m['text']}"

    def quote(self, m):
        label = self.debate_label(m)
        attr = f' context="{label}"' if label else ""
        return f'<message from="{LABEL.get(m["author"], m["author"])}"{attr}>\n{m["text"]}\n</message>'

    # ---- CLI drivers

    def command(self, chat, agent):
        s = chat["settings"]
        a = s["agents"][agent]
        session = chat["agents"][agent]["session"]
        if agent == "claude":
            cmd = [self.bins["claude"], "-p", "--output-format", "stream-json", "--verbose",
                   "--include-partial-messages"]
            if session:
                cmd += ["--resume", session]
            if a["model"]:
                cmd += ["--model", a["model"]]
            if a["effort"]:
                cmd += ["--effort", a["effort"]]
            # --tools only limits which tools exist; in dontAsk mode each one must also be pre-approved
            # with --allowedTools, or every call is refused (there's nobody to click "allow").
            read_only = "Read,Grep,Glob,WebSearch,WebFetch"
            cmd += {"none": ["--tools", ""],
                    "read": ["--tools", read_only, "--allowedTools", read_only, "--permission-mode", "dontAsk"],
                    "write": ["--allowedTools", read_only, "--permission-mode", "acceptEdits"]}[s["tools"]]
            return cmd
        cmd = [self.bins["codex"], "exec", "--json", "--skip-git-repo-check",
               "-s", "workspace-write" if s["tools"] == "write" else "read-only"]
        if a["model"]:
            cmd += ["-m", a["model"]]
        if a["effort"]:
            cmd += ["-c", f'model_reasoning_effort="{a["effort"]}"']
        cmd += ["resume", session, "-"] if session else ["-"]
        return cmd

    def run_cli(self, chat, agent, cmd, prompt, msg, stop):
        """Run one turn. Returns (session id, fatal error, non-fatal warnings)."""
        cid = chat["id"]
        proc = subprocess.Popen(cmd, cwd=chat["workdir"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True)
        with self.lock:
            self.procs[(cid, agent)] = proc
        errlines = []
        threading.Thread(target=lambda: errlines.extend(proc.stderr), daemon=True).start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        state = {"session": None, "error": None, "warnings": [], "last_push": 0.0}

        def add_text(chunk, new_block=False):
            with self.lock:
                if new_block and msg["text"].strip():
                    msg["text"] += "\n\n"
                msg["text"] += chunk
                if time.time() - state["last_push"] > 0.08:
                    state["last_push"] = time.time()
                    self.push(chat, msg)

        def activity(text):
            self.set_status(cid, agent, "running", text)

        handler = self.on_claude if agent == "claude" else self.on_codex
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                handler(ev, state, add_text, activity)
            proc.wait()
        finally:
            with self.lock:
                self.procs.pop((cid, agent), None)

        error = state["error"]
        if proc.returncode != 0 and not stop.is_set() and not error:
            tail = "".join(errlines[-15:]).strip()
            error = tail or f"{agent} exited with code {proc.returncode}"
        return state["session"], error, state["warnings"]

    @staticmethod
    def on_claude(ev, state, add_text, activity):
        t = ev.get("type")
        if ev.get("session_id"):
            state["session"] = ev["session_id"]
        if t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_start":
                block = e.get("content_block", {})
                if block.get("type") == "text":
                    add_text("", new_block=True)
                    activity("writing…")
                elif block.get("type") == "thinking":
                    activity("thinking…")
                elif block.get("type") in ("tool_use", "server_tool_use"):
                    activity(f"using {block.get('name', 'a tool')}…")
            elif e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                add_text(e["delta"]["text"])
        elif t == "result":
            if ev.get("is_error"):
                state["error"] = ev.get("result") or ev.get("subtype") or "Claude reported an error."
            state["result"] = ev.get("result", "")

    @staticmethod
    def on_codex(ev, state, add_text, activity):
        t = ev.get("type")
        if t == "thread.started":
            state["session"] = ev.get("thread_id")
        elif t in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item", {})
            kind = item.get("type")
            if kind == "agent_message" and t == "item.completed":
                add_text(item.get("text", ""), new_block=True)
                activity("writing…")
            elif kind == "reasoning":
                activity("thinking…")
            elif kind == "command_execution":
                activity(f"running: {item.get('command', '')[:80]}")
            elif kind == "web_search":
                activity(f"searching: {item.get('query', '')[:80]}")
            elif kind == "error":
                # Non-fatal notices, e.g. "resuming with a different model than the session was recorded with".
                if t == "item.completed" and item.get("message"):
                    state["warnings"].append(item["message"])
            elif kind:
                activity(f"{kind.replace('_', ' ')}…")
        elif t == "turn.failed":
            state["error"] = (ev.get("error") or {}).get("message", "Codex turn failed.")
        elif t == "error" and ev.get("message"):
            state["warnings"].append(ev["message"])


# --------------------------------------------------------------------------- http

ROUTES = [
    ("GET", r"/api/info", "info"),
    ("GET", r"/api/models", "models"),
    ("GET", r"/api/chats", "list"),
    ("POST", r"/api/chats", "create"),
    ("GET", r"/api/chats/(\w+)", "get"),
    ("PATCH", r"/api/chats/(\w+)", "patch"),
    ("DELETE", r"/api/chats/(\w+)", "delete"),
    ("GET", r"/api/chats/(\w+)/events", "events"),
    ("POST", r"/api/chats/(\w+)/send", "send"),
    ("POST", r"/api/chats/(\w+)/forward", "forward"),
    ("POST", r"/api/chats/(\w+)/debate", "debate"),
    ("POST", r"/api/chats/(\w+)/synthesize", "synthesize"),
    ("POST", r"/api/chats/(\w+)/stop", "stop"),
    ("POST", r"/api/chats/(\w+)/messages/(\w+)/retry", "retry"),
    ("GET", r"/api/chats/(\w+)/messages/(\w+)/prompt", "prompt"),
]
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        path = urlparse(self.path).path
        if method == "GET" and not path.startswith("/api/"):
            return self.static(path)
        for m, pattern, name in ROUTES:
            match = re.fullmatch(pattern, path)
            if m == method and match:
                try:
                    return getattr(self, f"h_{name}")(*match.groups())
                except KeyError:
                    return self.json({"error": "Not found"}, 404)
                except (ValueError, TypeError) as e:
                    return self.json({"error": str(e)}, 400)
        self.json({"error": "Not found"}, 404)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def json(self, data, code=200):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def static(self, path):
        f = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if not f.is_relative_to(STATIC) or not f.is_file():
            f = STATIC / "index.html"
        raw = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    # ---- handlers

    def h_info(self):
        self.json({"bins": self.app.bins, "versions": self.app.versions})

    def h_models(self):
        self.json(models())

    def h_list(self):
        self.json(self.app.list_chats())

    def h_create(self):
        self.json(self.app.new_chat(self.body().get("workdir")))

    def h_get(self, cid):
        with self.app.lock:
            self.json({"chat": self.app.public(self.app.chat(cid)), "status": self.app.statuses(cid)})

    def h_patch(self, cid):
        self.json(self.app.meta(self.app.update_chat(cid, self.body())))

    def h_delete(self, cid):
        self.app.chat(cid)
        self.app.delete_chat(cid)
        self.json({"ok": True})

    def h_send(self, cid):
        b = self.body()
        self.app.chat(cid)
        self.app.send(cid, b.get("text", ""), b.get("to"))
        self.json({"ok": True})

    def h_forward(self, cid):
        b = self.body()
        self.app.forward(cid, b["message_id"], b["to"], b.get("note", ""))
        self.json({"ok": True})

    def h_debate(self, cid):
        b = self.body()
        self.app.chat(cid)
        self.app.debate(cid, b.get("rounds", 2), b.get("first", "claude"), b.get("topic", ""))
        self.json({"ok": True})

    def h_retry(self, cid, mid):
        self.app.retry(cid, mid)
        self.json({"ok": True})

    def h_prompt(self, cid, mid):
        with self.app.lock:
            sent = self.app.chat(cid).get("prompts", {}).get(mid)
        if not sent:
            return self.json({"error": "No prompt recorded for this message (it may predate this feature)."}, 404)
        self.json({"prompt": sent["prompt"], "command": sent["command"]})

    def h_synthesize(self, cid):
        self.app.chat(cid)
        self.app.synthesize(cid, self.body().get("by", "claude"))
        self.json({"ok": True})

    def h_stop(self, cid):
        self.app.stop(cid)
        self.json({"ok": True})

    def h_events(self, cid):
        app = self.app
        with app.lock:
            snapshot = {"type": "snapshot", "chat": app.public(app.chat(cid)), "status": app.statuses(cid)}
            q = app.hub.subscribe(cid)
            first = json.dumps(snapshot)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(f"data: {first}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    with app.lock:
                        data = json.dumps(ev)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            app.hub.unsubscribe(cid, q)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=os.environ.get("MULTICHAT_DATA", str(ROOT / "data")))
    ap.add_argument("--open", action="store_true", help="open the UI in your browser")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    Handler.app = App(Path(args.data))
    for agent in AGENTS:
        v = Handler.app.versions[agent]
        print(f"  {LABEL[agent]:7} {Handler.app.bins[agent]}  ({v or 'NOT WORKING — check the binary'})")
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on {args.host}:{args.port} ({e.strerror}). "
                 f"Is multi-chat already running? Otherwise pick another port with --port.")
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}"
    print(f"multi-chat on {url}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p in list(Handler.app.procs.values()):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    main()
