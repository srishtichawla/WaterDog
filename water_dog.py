"""
Water Dog - a dog cutout that sits on the edge of your screen.
Sad when it is water time, ANGRY if you ignore it, happy when you drink.
Click the dog when you have had water. Right-click the dog = later.
Also pings your phone through ntfy.
"""

import datetime
import json
import math
import queue
import random
import signal
import string
import subprocess
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBezierPath,
                    NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
                    NSPanel, NSRectFillUsingOperation, NSScreen, NSView)
from Foundation import NSMakePoint, NSMakeRect, NSObject, NSString, NSTimer

# ===================== SETTINGS - edit these =====================
REMIND_EVERY_SEC = 1800
ANGRY_AFTER_SEC = 180
ANGRY_REPEAT_SEC = 180
DAILY_GOAL = 8             # glasses per day
QUIET_BEFORE_HOUR = 8      # no reminders before 8am
QUIET_AFTER_HOUR = 23      # no reminders after 11pm
PHONE_MIN_GAP_SEC = 25     # minimum gap between phone pushes (ntfy.sh limits free use)
# =================================================================

SERVER = "https://ntfy.sh"
STATE_PATH = Path.home() / ".water_dog.json"
W, H = 250, 245            # size of the floating dog window
DOG_CX, DOG_CY = 125, 150  # where the dog sits inside it


def save_state(st):
    try:
        STATE_PATH.write_text(json.dumps(st, indent=2))
    except Exception:
        pass


def load_state():
    st = {}
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text())
        except Exception:
            st = {}
    if "topic" not in st:
        rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        st["topic"] = "water-dog-" + rand
    st.setdefault("count", 0)
    st.setdefault("date", "")
    save_state(st)
    return st


def play(sound):
    try:
        subprocess.Popen(["afplay", "/System/Library/Sounds/" + sound + ".aiff"])
    except Exception:
        pass


def col(h, a=1.0):
    h = h.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def paint_path(p, fill=None, stroke=None, lw=1.0):
    if fill is not None:
        fill.set()
        p.fill()
    if stroke is not None:
        stroke.set()
        p.setLineWidth_(max(1.0, lw))
        p.stroke()


def text_attrs(size, color, bold):
    if bold:
        font = NSFont.boldSystemFontOfSize_(size)
    else:
        font = NSFont.systemFontOfSize_(size)
    return {NSFontAttributeName: font, NSForegroundColorAttributeName: color}


def text_size(txt, size, bold=False):
    sz = NSString.stringWithString_(txt).sizeWithAttributes_(
        text_attrs(size, NSColor.blackColor(), bold))
    return sz.width, sz.height


def draw_text(txt, cx, cy, size, color, bold=False):
    w, h = text_size(txt, size, bold)
    NSString.stringWithString_(txt).drawAtPoint_withAttributes_(
        NSMakePoint(cx - w / 2.0, cy - h / 2.0), text_attrs(size, color, bold))


class DogView(NSView):
    """Transparent view. Top-left origin, so coordinates work like a normal screen."""

    def isFlipped(self):
        return True

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True

    def drawRect_(self, rect):
        self.owner.paint()

    def mouseDown_(self, event):
        if event.modifierFlags() & (1 << 18 | 1 << 19):   # Ctrl-click or Option-click = ignore
            self.owner.later()
        else:
            self.owner.clicked()

    def rightMouseDown_(self, event):
        self.owner.later()


class Ticker(NSObject):
    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            traceback.print_exc()


class WaterDog:
    def __init__(self):
        self.st = load_state()
        self.topic = self.st["topic"]
        self.reply_topic = self.topic + "-reply"
        self.q = queue.Queue()
        self.mood = "sad"
        self.msg = ""
        self.sub = ""
        self.visible = False
        self.frame = 0
        self.remind_at = None
        self.angry_at = None
        self.hide_at = None
        self.force_next = False
        self.last_push = 0.0
        self.last_front = 0.0
        try:
            from Foundation import NSProcessInfo
            self.activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                0x00EFFFFF, "Water Dog reminders")
        except Exception as e:
            print("Could not disable App Nap:", e)

        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # Borderless, see-through, always-on-top panel that never steals focus
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), 128, 2, False)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(False)
        self.panel.setLevel_(25)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        # all Spaces + visible over full-screen apps
        self.panel.setCollectionBehavior_(1 | 16 | 64 | 256)

        self.view = DogView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self.view.owner = self
        self.panel.setContentView_(self.view)

        self.ticker = Ticker.alloc().init()
        self.ticker.owner = self
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self.ticker, "tick:", None, True)

        threading.Thread(target=self.listen_phone, daemon=True).start()

        # First reminder a few seconds after start (ignores quiet hours) so you see it work
        self.force_next = True
        self.remind_at = time.time() + 3
        self.notify("Water Dog is running", "Reminders will show up here too.",
                    ["dog"], 2, False, False)

    # ---------- window ----------
    def show(self):
        vf = NSScreen.mainScreen().visibleFrame()
        x = vf.origin.x + vf.size.width - W     # flush with the right edge
        y = vf.origin.y                         # sitting on the bottom edge / Dock
        self.panel.setFrameOrigin_(NSMakePoint(x, y))
        self.panel.orderFrontRegardless()
        self.visible = True

    def hide(self):
        self.visible = False
        self.panel.orderOut_(None)

    def set_mood(self, mood, msg):
        today = str(datetime.date.today())
        if self.st.get("date") == today:
            pass
        else:
            self.st["date"] = today
            self.st["count"] = 0
            save_state(self.st)
        self.mood = mood
        self.msg = msg
        self.sub = "Today: %d/%d glasses 💧" % (self.st["count"], DAILY_GOAL)

    # ---------- phone (ntfy) ----------
    def notify(self, title, message, tags, priority=3, actions=True, throttle=True):
        now = time.time()
        if throttle and now - self.last_push < PHONE_MIN_GAP_SEC:
            return
        if throttle:
            self.last_push = now
        body = {"topic": self.topic, "title": title, "message": message,
                "tags": tags, "priority": priority}
        if actions:
            body["actions"] = [{
                "action": "http", "label": "I drank 💧",
                "url": SERVER + "/" + self.reply_topic,
                "method": "POST", "body": "drank", "clear": True,
            }]

        def run():
            try:
                req = urllib.request.Request(
                    SERVER + "/", data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                print("Phone notification failed:", e)

        threading.Thread(target=run, daemon=True).start()

    def listen_phone(self):
        """Listens for the 'I drank' button tapped on the phone notification."""
        since = int(time.time())
        seen = set()
        while True:
            try:
                url = SERVER + "/" + self.reply_topic + "/json?since=" + str(since)
                with urllib.request.urlopen(url, timeout=120) as r:
                    for line in r:
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue
                        if ev.get("event") == "message" and ev.get("id") not in seen:
                            seen.add(ev["id"])
                            since = ev.get("time", since)
                            self.q.put("drank")
            except Exception:
                time.sleep(5)

    # ---------- the clock ----------
    def tick(self):
        now = time.time()
        self.frame += 1
        try:
            while True:
                if self.q.get_nowait() == "drank":
                    self.drank()
        except queue.Empty:
            pass
        if self.remind_at and now >= self.remind_at:
            self.remind_at = None
            self.remind()
        if self.angry_at and now >= self.angry_at:
            self.angry()
        if self.hide_at and now >= self.hide_at:
            self.hide_at = None
            self.hide()
        if self.visible:
            self.view.setNeedsDisplay_(True)
            if now - self.last_front > 1.5:
                self.last_front = now
                self.panel.orderFrontRegardless()

    # ---------- dog states ----------
    def remind(self):
        now = datetime.datetime.now()
        force = self.force_next
        self.force_next = False
        if not force and not (QUIET_BEFORE_HOUR <= now.hour < QUIET_AFTER_HOUR):
            print("[%s] Quiet hours - skipping, checking again in 10 min" % now.strftime("%H:%M:%S"))
            self.remind_at = time.time() + 600
            return
        print("[%s] Reminder" % now.strftime("%H:%M:%S"))
        self.set_mood("sad", "Water time, please 💧")
        self.show()
        play("Pop")
        self.notify("Woof - water time 💧", "Your dog is waiting for you to drink water 🐶",
                    ["dog", "droplet"])
        self.angry_at = time.time() + ANGRY_AFTER_SEC

    def angry(self):
        print("[%s] Dog is ANGRY" % datetime.datetime.now().strftime("%H:%M:%S"))
        self.set_mood("angry", "GRRR drink water NOW 😠")
        self.show()
        play("Basso")
        self.notify("GRRR 😠", "You ignored me. Drink water RIGHT NOW",
                    ["rage", "droplet"], 4)
        self.angry_at = time.time() + ANGRY_REPEAT_SEC

    def later(self):
        if self.mood == "happy":
            self.hide()
            return
        print("[%s] Ignored - dog will be ANGRY in %d sec" % (
            datetime.datetime.now().strftime("%H:%M:%S"), ANGRY_AFTER_SEC))
        self.hide()
        self.angry_at = time.time() + ANGRY_AFTER_SEC   # fresh countdown

    def clicked(self):
        if self.mood == "happy":
            return
        self.drank()

    def drank(self):
        self.angry_at = None
        self.st["count"] = self.st.get("count", 0) + 1
        save_state(self.st)
        print("[%s] Drank water - %d/%d today, next reminder in %d sec" % (
            datetime.datetime.now().strftime("%H:%M:%S"),
            self.st["count"], DAILY_GOAL, REMIND_EVERY_SEC))
        self.set_mood("happy", "Yaaay good human 💙")
        self.show()
        play("Glass")
        self.notify("Yay 🎉", "Woof woof - that's %d/%d glasses today." % (self.st["count"], DAILY_GOAL),
                    ["dog", "tada"], 2, False)
        self.hide_at = time.time() + 4
        self.remind_at = time.time() + REMIND_EVERY_SEC

    # ---------- drawing ----------
    def paint(self):
        NSRectFillUsingOperation(NSMakeRect(0, 0, W, H), 0)   # wipe to fully transparent
        mood = self.mood
        s = 1.0 if mood == "angry" else 0.85
        dx = dy = 0.0
        if mood == "happy":
            dy = -abs(math.sin(self.frame / 4.0)) * 16
        elif mood == "angry":
            dx = math.sin(self.frame * 1.5) * 6
        else:
            dy = math.sin(self.frame / 6.0) * 3

        def X(x):
            return DOG_CX + (x - 150) * s + dx

        def Y(y):
            return DOG_CY + (y - 140) * s + dy

        def oval(x1, y1, x2, y2, fill, stroke=None, lw=1.0):
            p = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(X(x1), Y(y1), (x2 - x1) * s, (y2 - y1) * s))
            paint_path(p, fill, stroke, lw * s)

        def line(x1, y1, x2, y2, color, lw):
            p = NSBezierPath.bezierPath()
            p.moveToPoint_(NSMakePoint(X(x1), Y(y1)))
            p.lineToPoint_(NSMakePoint(X(x2), Y(y2)))
            p.setLineCapStyle_(1)
            paint_path(p, None, color, lw * s)

        def arc(x1, y1, x2, y2, start, extent, color, lw):
            mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            rx, ry = (x2 - x1) / 2.0, (y2 - y1) / 2.0
            p = NSBezierPath.bezierPath()
            n = 24
            for i in range(n + 1):
                a = math.radians(start + extent * i / float(n))
                pt = NSMakePoint(X(mx + rx * math.cos(a)), Y(my - ry * math.sin(a)))
                if i == 0:
                    p.moveToPoint_(pt)
                else:
                    p.lineToPoint_(pt)
            p.setLineCapStyle_(1)
            p.setLineJoinStyle_(1)
            paint_path(p, None, color, lw * s)

        # ---- text-message bubble above the dog ----
        if mood == "angry":
            b_fill, b_text, b_sub, b_edge = col("#FF453A"), col("#FFFFFF"), col("#FFE3E0"), col("#B3261E")
        elif mood == "happy":
            b_fill, b_text, b_sub, b_edge = col("#32D74B"), col("#FFFFFF"), col("#E6FFEA"), col("#1E8E3E")
        else:
            b_fill, b_text, b_sub, b_edge = col("#EDEDF0"), col("#111111"), col("#6E6E73"), col("#A8A8AD")
        w1, h1 = text_size(self.msg, 14, True)
        w2, h2 = text_size(self.sub, 11)
        bw = min(W - 8, max(w1, w2) + 30)
        bh = h1 + h2 + 18
        bx = (W - bw) / 2.0
        by = 5
        tail = NSBezierPath.bezierPath()
        tail.moveToPoint_(NSMakePoint(W / 2.0 - 12, by + bh - 2))
        tail.lineToPoint_(NSMakePoint(W / 2.0 + 12, by + bh - 2))
        tail.lineToPoint_(NSMakePoint(W / 2.0, by + bh + 16))
        tail.closePath()
        paint_path(tail, b_fill, b_edge, 1.5)
        bub = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by, bw, bh), 16, 16)
        paint_path(bub, b_fill, b_edge, 1.5)
        hide_seam = NSBezierPath.bezierPathWithRect_(NSMakeRect(W / 2.0 - 10.5, by + bh - 1.5, 21, 3))
        paint_path(hide_seam, b_fill)
        draw_text(self.msg, W / 2.0, by + 9 + h1 / 2.0, 14, b_text, True)
        draw_text(self.sub, W / 2.0, by + 9 + h1 + h2 / 2.0, 11, b_sub)

        # ---- the dog ----
        OUT, BLACK, WHITE = col("#5A3A1A"), col("#222222"), col("#FFFFFF")
        fur = col("#E8896A") if mood == "angry" else col("#D9A066")
        ear = col("#B0642F") if mood == "angry" else col("#8B5A2B")

        oval(40, 50, 105, 170, ear, OUT, 3)
        oval(195, 50, 260, 170, ear, OUT, 3)
        oval(65, 55, 235, 225, fur, OUT, 3)
        oval(105, 135, 195, 215, col("#FBE8D0"), OUT, 2)
        oval(138, 145, 162, 162, BLACK)

        if mood == "happy":
            arc(90, 95, 130, 130, 0, 180, BLACK, 5)
            arc(170, 95, 210, 130, 0, 180, BLACK, 5)
            oval(75, 130, 105, 150, col("#FF9EB5"))
            oval(195, 130, 225, 150, col("#FF9EB5"))
            oval(142, 188, 160, 210, col("#FF6F91"), BLACK, 2)
            arc(125, 160, 175, 200, 200, 140, BLACK, 3)
            draw_text("✨", X(30), Y(55), 22, BLACK)
            draw_text("💙", X(272), Y(75), 22, BLACK)
        elif mood == "angry":
            oval(95, 102, 125, 130, BLACK)
            oval(175, 102, 205, 130, BLACK)
            line(85, 90, 132, 108, BLACK, 6)
            line(215, 90, 168, 108, BLACK, 6)
            arc(125, 182, 175, 218, 20, 140, BLACK, 4)
            draw_text("💢", X(252), Y(40), 28, BLACK)
        else:
            for ex in (90, 175):
                oval(ex, 95, ex + 35, 130, WHITE, BLACK, 2)
                oval(ex + 9, 104, ex + 27, 124, BLACK)
                oval(ex + 15, 108, ex + 21, 114, WHITE)
            line(85, 92, 125, 80, BLACK, 4)
            line(215, 92, 175, 80, BLACK, 4)
            arc(135, 178, 165, 198, 20, 140, BLACK, 3)
            draw_text("💧", X(258), Y(60), 22, BLACK)


def main():
    dog = WaterDog()
    print("=" * 56)
    print("Water Dog is running (native cutout mode)")
    print("Phone: ntfy app -> + -> subscribe to topic:  " + dog.topic)
    print("Reminder every %d sec, angry after %d more sec" % (REMIND_EVERY_SEC, ANGRY_AFTER_SEC))
    print("Quiet hours: before %d:00 and after %d:00" % (QUIET_BEFORE_HOUR, QUIET_AFTER_HOUR))
    print("Click the dog = I drank water | Right-click = later | Ctrl+C here = quit")
    print("=" * 56)
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # lets Ctrl+C quit
    dog.app.run()


if __name__ == "__main__":
    main()
