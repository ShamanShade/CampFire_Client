"""
Profile effects (animated particles over a profile card) and avatar decorations (SVG frames
around an avatar, from the decorations/ folder). web/app.js draws the same effects on a canvas,
with the same rules, so the app and the website look alike.
"""

from __future__ import annotations

import math
import os
import random
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DECO_DIR = os.path.join(BASE, "decorations")

DECORATIONS = [("", "None"), ("flame", "Campfire flames"), ("gold", "Gold ring"), ("neon", "Neon"),
               ("rainbow", "Rainbow"), ("crown", "Crown"), ("halo", "Halo"), ("cat", "Cat ears"),
               ("hearts", "Hearts"), ("frost", "Frost"), ("leaves", "Leaf wreath")]
EFFECTS = [("", "None"), ("snow", "Snow"), ("sparkles", "Sparkles"), ("hearts", "Floating hearts"),
           ("embers", "Campfire embers"), ("confetti", "Confetti"), ("sakura", "Cherry blossoms"),
           ("bubbles", "Bubbles"), ("stars", "Starry night")]
AVATAR_SHARE = 0.8              # the avatar is the middle 80% of a decoration's square

_renderers: dict = {}


def deco_renderer(deco):
    """A QSvgRenderer for a decoration (None if unknown)."""
    if not deco:
        return None
    if deco not in _renderers:
        from PyQt6.QtSvg import QSvgRenderer
        path = os.path.join(DECO_DIR, f"{deco}.svg")
        r = QSvgRenderer(path) if os.path.exists(path) else None
        _renderers[deco] = r if r is not None and r.isValid() else None
    return _renderers[deco]


def decorate(avatar: QImage, deco: str, box: int) -> QImage:
    """A box x box picture: the (round) avatar in the middle 80%, the decoration around it."""
    r = deco_renderer(deco)
    if r is None:
        return avatar
    dpr = 2
    out = QImage(box * dpr, box * dpr, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    side = box * dpr * AVATAR_SHARE
    off = (box * dpr - side) / 2
    p.drawImage(QRectF(off, off, side, side), avatar)
    r.render(p, QRectF(0, 0, box * dpr, box * dpr))
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


# ------------------------------------------------------------------ particles
COLORS = {
    "snow": ["#ffffff", "#e3f2fd", "#bbdefb"],
    "sparkles": ["#fff59d", "#ffffff", "#ffe082", "#b3e5fc"],
    "hearts": ["#ff4f8b", "#ff8fb7", "#ff6f91", "#ffb3cf"],
    "embers": ["#ff6d00", "#ffab40", "#ffd180", "#ff3d00"],
    "confetti": ["#ff5252", "#ffd740", "#69f0ae", "#40c4ff", "#e040fb", "#ff6e40"],
    "sakura": ["#f8bbd0", "#f48fb1", "#fce4ec"],
    "bubbles": ["#b3e5fc", "#e1f5fe", "#80deea"],
    "stars": ["#ffffff", "#fff9c4", "#e1f5fe"],
}
COUNT = {"snow": 40, "sparkles": 22, "hearts": 16, "embers": 30, "confetti": 40, "sakura": 18, "bubbles": 16, "stars": 34}


def spawn(effect, w, h, rnd, start=False):
    """A new particle (a dict). start: spread over the whole card instead of entering at an edge."""
    c = rnd.choice(COLORS[effect])
    p = {"x": rnd.uniform(0, w), "y": 0.0, "vx": 0.0, "vy": 0.0, "s": 1.0, "rot": rnd.uniform(0, 360),
         "vr": 0.0, "age": 0.0, "life": 1e9, "c": c, "ph": rnd.uniform(0, 6.28)}
    if effect == "snow":
        p.update(y=rnd.uniform(-h, h) if start else -6, vy=rnd.uniform(18, 45), s=rnd.uniform(1.2, 3.2))
    elif effect == "sparkles":
        p.update(y=rnd.uniform(0, h), s=rnd.uniform(3, 7), life=rnd.uniform(0.8, 1.8),
                 age=rnd.uniform(0, 1.5) if start else 0)
    elif effect == "hearts":
        p.update(y=rnd.uniform(0, h) if start else h + 10, vy=-rnd.uniform(22, 50), s=rnd.uniform(5, 10),
                 vr=rnd.uniform(-20, 20))
    elif effect == "embers":
        p.update(y=rnd.uniform(0, h) if start else h + 4, vy=-rnd.uniform(25, 60), s=rnd.uniform(1.2, 3),
                 life=rnd.uniform(2.5, 5), age=rnd.uniform(0, 2) if start else 0)
    elif effect == "confetti":
        p.update(y=rnd.uniform(-h, h) if start else -8, vy=rnd.uniform(40, 80), vx=rnd.uniform(-15, 15),
                 s=rnd.uniform(3, 6), vr=rnd.uniform(-240, 240))
    elif effect == "sakura":
        p.update(y=rnd.uniform(-h, h) if start else -8, vy=rnd.uniform(20, 40), vx=rnd.uniform(10, 30),
                 x=rnd.uniform(-w * 0.3, w), s=rnd.uniform(4, 7), vr=rnd.uniform(-90, 90))
    elif effect == "bubbles":
        p.update(y=rnd.uniform(0, h) if start else h + 10, vy=-rnd.uniform(15, 35), s=rnd.uniform(3, 9))
    elif effect == "stars":
        p.update(y=rnd.uniform(0, h), s=rnd.uniform(0.8, 2.2), life=rnd.uniform(2, 5),
                 age=rnd.uniform(0, 4) if start else 0)
    return p


def step(effect, parts, dt, w, h, rnd):
    for i, p in enumerate(parts):
        p["age"] += dt
        sway = math.sin(p["age"] * 1.6 + p["ph"])
        p["x"] += (p["vx"] + (sway * 12 if effect in ("snow", "embers", "bubbles", "sakura") else 0)) * dt
        p["y"] += p["vy"] * dt
        p["rot"] += p["vr"] * dt
        gone = p["age"] > p["life"] or p["y"] > h + 12 or p["y"] < -14 or p["x"] > w + 14
        if gone:
            parts[i] = spawn(effect, w, h, rnd)


def alpha(effect, p) -> float:
    """How visible a particle is now (fading in and out)."""
    if p["life"] < 1e8:
        t = p["age"] / p["life"]
        return max(0.0, math.sin(math.pi * min(1.0, t)))
    if effect == "hearts" or effect == "bubbles":
        return 0.9
    return 0.85


def heart_path(size):
    path = QPainterPath(QPointF(0, size * 0.3))
    path.cubicTo(-size * 0.6, -size * 0.3, -size * 1.1, size * 0.4, 0, size)
    path.cubicTo(size * 1.1, size * 0.4, size * 0.6, -size * 0.3, 0, size * 0.3)
    return path


def star_path(size, points=4, inner=0.35):
    path = QPainterPath()
    for k in range(points * 2):
        r = size if k % 2 == 0 else size * inner
        a = math.pi * k / points - math.pi / 2
        pt = QPointF(math.cos(a) * r, math.sin(a) * r)
        path.moveTo(pt) if k == 0 else path.lineTo(pt)
    path.closeSubpath()
    return path


def draw(effect, parts, painter: QPainter):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for p in parts:
        a = alpha(effect, p)
        if a <= 0.01:
            continue
        c = QColor(p["c"])
        painter.save()
        painter.translate(p["x"], p["y"])
        painter.rotate(p["rot"] if effect in ("confetti", "sakura", "hearts") else 0)
        painter.setOpacity(a)
        s = p["s"]
        if effect in ("snow", "stars"):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawEllipse(QPointF(0, 0), s, s)
        elif effect == "sparkles":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawPath(star_path(s * (0.6 + 0.4 * a)))
        elif effect == "hearts":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawPath(heart_path(s))
        elif effect == "embers":
            g = QRadialGradient(QPointF(0, 0), s * 3)
            g.setColorAt(0, c)
            glow = QColor(c)
            glow.setAlpha(0)
            g.setColorAt(1, glow)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(g)
            painter.drawEllipse(QPointF(0, 0), s * 3, s * 3)
        elif effect == "confetti":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRect(QRectF(-s / 2, -s, s, s * 2 * abs(math.cos(math.radians(p["rot"] * 2))) + 0.5))
        elif effect == "sakura":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawEllipse(QPointF(0, 0), s, s * 0.55)
        elif effect == "bubbles":
            painter.setPen(QPen(c, 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), s, s)
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
            painter.drawArc(QRectF(-s * 0.6, -s * 0.6, s * 0.7, s * 0.7), 100 * 16, 70 * 16)
        painter.restore()


class EffectLayer(QWidget):
    """Draws a profile effect over its parent; clicks go through."""

    FPS = 30

    def __init__(self, parent, effect=""):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.rnd = random.Random()
        self.effect = ""
        self.parts: list = []
        self.timer = QTimer(self, interval=1000 // self.FPS, timeout=self.tick)
        self.set_effect(effect)

    def set_effect(self, effect):
        self.effect = effect if effect in COUNT else ""
        w, h = max(1, self.width()), max(1, self.height())
        self.parts = [spawn(self.effect, w, h, self.rnd, start=True) for _ in range(COUNT.get(self.effect, 0))]
        self.timer.start() if self.effect else self.timer.stop()
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.effect and ev.oldSize().width() <= 1:
            self.set_effect(self.effect)

    def tick(self):
        if not self.isVisible():
            return
        step(self.effect, self.parts, 1 / self.FPS, self.width(), self.height(), self.rnd)
        self.update()

    def paintEvent(self, _ev):
        if self.effect:
            p = QPainter(self)
            draw(self.effect, self.parts, p)
            p.end()
