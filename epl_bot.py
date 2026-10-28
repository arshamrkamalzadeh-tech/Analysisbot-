"""
بات تحلیل بازی‌های لیگ برتر انگلیس (سروش پلاس + VictoryAPI)

اجرا:
    pip install requests
    export BOT_TOKEN="توکن بات سروش پلاس"
    export VICTORY_API_KEY="کلید VictoryAPI"
    python epl_bot.py

همه‌چیز با دکمه‌های شیشه‌ای است؛ تنها دستور اسلشی /start است.

استیکرها (اختیاری):
    export ADMIN_ID="شناسه عددی شما"      # از دکمه «شناسه من» در بات بگیرید
    export STICKER_SET="نام پک استیکر"    # اگر نام پک را می‌دانید
    سپس هر استیکری که ادمین برای بات بفرستد ذخیره می‌شود (stickers.json).
"""
import difflib
import html
import json
import math
import os
import random
import re
import time
from datetime import datetime

import requests

# ------------------------- تنظیمات -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
VICTORY_KEY = os.environ.get("VICTORY_API_KEY", "")
SPLUS_BASE = os.environ.get("SPLUS_API_BASE", "https://api.splus.ir/bot{token}")
VICTORY_BASE = "https://api.v1.victoryapi.ir"
LEAGUE_ID = 39  # لیگ برتر انگلیس
STANDINGS_TTL = 6 * 3600
H2H_TTL = 24 * 3600
STICKER_TTL = 12 * 3600
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
STICKER_SET = os.environ.get("STICKER_SET", "").strip()
STICKER_FILE = os.environ.get("STICKER_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "stickers.json")
SHRINK_K = 5  # هرچه بزرگ‌تر، آمار اوایل فصل بیشتر به میانگین لیگ نزدیک می‌شود

ALIASES = {  # نام‌های رایج -> بخشی از نام تیم در API
    "man utd": "manchester united", "man united": "manchester united", "united": "manchester united",
    "man city": "manchester city", "city": "manchester city",
    "spurs": "tottenham", "wolves": "wolverhampton", "newcastle united": "newcastle",
    "forest": "nottingham forest", "nottm forest": "nottingham forest",
    "villa": "aston villa", "palace": "crystal palace", "west ham united": "west ham",
    "آرسنال": "arsenal", "چلسی": "chelsea", "لیورپول": "liverpool",
    "منچستریونایتد": "manchester united", "منچسترسیتی": "manchester city",
    "تاتنهام": "tottenham", "نیوکاسل": "newcastle", "اورتون": "everton",
    "استونویلا": "aston villa", "برایتون": "brighton", "وستهام": "west ham",
    "فولام": "fulham", "برنتفورد": "brentford", "کریستالپالاس": "crystal palace",
    "ولورهمپتون": "wolverhampton", "ناتینگهام": "nottingham forest",
    "بورنموث": "bournemouth", "لیدز": "leeds", "برنلی": "burnley", "سندرلند": "sunderland",
}


# ------------------------- ابزارهای کمکی -------------------------
def norm(s: str) -> str:
    s = s.lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", "")
    return re.sub(r"[^a-z0-9\u0600-\u06ff]", "", s)


def current_season() -> int:
    now = datetime.now()
    return now.year if now.month >= 7 else now.year - 1


_cache: dict = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def victory_get(path: str, params: dict) -> dict:
    r = requests.get(f"{VICTORY_BASE}/{path}", params=params,
                     headers={"x-vapi-key": VICTORY_KEY}, timeout=20)
    if r.status_code == 204:
        return {"response": []}
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"VictoryAPI: {data['errors']}")
    return data


# ------------------------- دریافت داده -------------------------
def fetch_standings(season: int) -> list:
    rows, page = [], 1
    while True:
        d = victory_get("Standings", {"leagueId": LEAGUE_ID, "season": season,
                                      "pageNumber": page, "pageSize": 50})
        for lg in d.get("response", []):
            for group in lg["league"]["standings"]:
                rows.extend(group)
        total = (d.get("paging") or {}).get("total", 1)
        if page >= total:
            break
        page += 1
    return rows


def get_table() -> list:
    season = current_season()

    def load():
        rows = fetch_standings(season)
        return rows or fetch_standings(season - 1)  # اگر فصل جدید هنوز خالی بود
    return cached("standings", STANDINGS_TTL, load)


def find_team(text: str, table: list):
    q = norm(text)
    q = norm(ALIASES.get(q, ALIASES.get(text.strip().lower(), text)))
    names = {norm(r["team"]["name"]): r for r in table}
    if q in names:
        return names[q]
    for n, r in names.items():  # شامل بودن
        if len(q) >= 4 and (q in n or n in q):
            return r
    m = difflib.get_close_matches(q, list(names), n=1, cutoff=0.78)
    return names[m[0]] if m else None


def fetch_h2h(home_id: int, away_id: int) -> list:
    def load():
        d = victory_get("fixtures/headtohead", {"teamHomeId": home_id, "teamAwayId": away_id})
        done = [f for f in d.get("response", [])
                if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")
                and f["goals"]["home"] is not None]
        done.sort(key=lambda f: f["fixture"]["timestamp"], reverse=True)
        return done[:5]
    return cached(f"h2h:{home_id}:{away_id}", H2H_TTL, load)


# ------------------------- مدل پیش‌بینی -------------------------
def poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def shrunk(goals: float, played: int, league_avg: float) -> float:
    return (goals + SHRINK_K * league_avg) / (played + SHRINK_K)


def form_ratio(form: str) -> float:
    form = (form or "")[-5:]
    if not form:
        return 0.5
    pts = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form)
    return pts / (3 * len(form))


def predict(home: dict, away: dict, table: list) -> dict:
    hp = sum(r["home"]["played"] for r in table) or 1
    avg_home = sum(r["home"]["goals"]["for"] for r in table) / hp
    avg_away = sum(r["away"]["goals"]["for"] for r in table) / hp

    h, a = home["home"], away["away"]
    h_att = shrunk(h["goals"]["for"], h["played"], avg_home) / avg_home
    h_def = shrunk(h["goals"]["against"], h["played"], avg_away) / avg_away
    a_att = shrunk(a["goals"]["for"], a["played"], avg_away) / avg_away
    a_def = shrunk(a["goals"]["against"], a["played"], avg_home) / avg_home

    lam_h = avg_home * h_att * a_def * (1 + 0.2 * (form_ratio(home.get("form")) - 0.5))
    lam_a = avg_away * a_att * h_def * (1 + 0.2 * (form_ratio(away.get("form")) - 0.5))

    n = 9
    grid = [[poisson(i, lam_h) * poisson(j, lam_a) for j in range(n)] for i in range(n)]
    p_home = sum(grid[i][j] for i in range(n) for j in range(n) if i > j)
    p_draw = sum(grid[i][i] for i in range(n))
    p_away = sum(grid[i][j] for i in range(n) for j in range(n) if i < j)
    tot = p_home + p_draw + p_away
    scores = sorted(((grid[i][j] / tot, i, j) for i in range(n) for j in range(n)), reverse=True)[:3]
    over25 = sum(grid[i][j] for i in range(n) for j in range(n) if i + j > 2) / tot
    btts = sum(grid[i][j] for i in range(1, n) for j in range(1, n)) / tot
    return dict(lam_h=lam_h, lam_a=lam_a, home=p_home / tot, draw=p_draw / tot,
                away=p_away / tot, scores=scores, over25=over25, btts=btts)


# ------------------------- ظاهر و متن‌ها -------------------------
FA_NAMES = {  # نام انگلیسی (نرمال‌شده) -> نام فارسی
    "manchesterunited": "منچستر یونایتد", "manchestercity": "منچستر سیتی",
    "arsenal": "آرسنال", "chelsea": "چلسی", "liverpool": "لیورپول", "tottenham": "تاتنهام",
    "newcastle": "نیوکاسل", "everton": "اورتون", "astonvilla": "استون ویلا",
    "brighton": "برایتون", "westham": "وست هام", "fulham": "فولام", "brentford": "برنتفورد",
    "crystalpalace": "کریستال پالاس", "wolverhampton": "ولورهمپتون", "wolves": "ولورهمپتون",
    "nottinghamforest": "ناتینگهام فارست", "bournemouth": "بورنموث", "leeds": "لیدز",
    "burnley": "برنلی", "sunderland": "سندرلند", "leicester": "لستر", "southampton": "ساوتهمپتون",
    "ipswich": "ایپسویچ", "westbrom": "وست برومویچ", "sheffieldunited": "شفیلد یونایتد",
    "luton": "لوتون", "norwich": "نوریچ", "watford": "واتفورد",
}
LINE = "━━━━━━━━━━━━━━━"
FORM_ICON = {"W": "✅", "D": "➖", "L": "❌"}
MEDALS = ["🥇", "🥈", "🥉"]

# حالت‌های استیکر: ایموجی‌هایی که برای هر موقعیت ترجیح داده می‌شوند
MOODS = {
    "welcome": ["👋", "😍", "⚽", "🤩", "😊", "😃"],
    "hot": ["🔥", "🏆", "😎", "👏", "🤩", "💪"],
    "think": ["🤔", "🧐", "😎", "🤝"],
    "sad": ["😢", "😕", "🙈", "😅", "🥲"],
}


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def fa_name(row: dict) -> str:
    n = norm(row["team"]["name"])
    for key, fa in FA_NAMES.items():
        if key in n:
            return fa
    return row["team"]["name"]


def team_row(table: list, tid: int):
    return next((r for r in table if r["team"]["id"] == tid), None)


def bar(p: float, size: int = 10) -> str:
    n = max(0, min(size, round(p * size)))
    return "▰" * n + "▱" * (size - n)


def form_text(form: str) -> str:
    f = (form or "")[-5:]
    return " ".join(FORM_ICON.get(c, c) for c in f) or "نامشخص"


# ------------------------- استیکر -------------------------
def _clean(e: str) -> str:
    return (e or "").replace("\ufe0f", "")


def load_saved_stickers() -> list:
    try:
        with open(STICKER_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_sticker(file_id: str, emoji: str) -> int:
    items = load_saved_stickers()
    os.makedirs(os.path.dirname(STICKER_FILE) or ".", exist_ok=True)
    if not any(s.get("file_id") == file_id for s in items):
        items.append({"file_id": file_id, "emoji": emoji or ""})
        with open(STICKER_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    return len(items)


def sticker_pool() -> list:
    pool = list(load_saved_stickers())
    if STICKER_SET:
        def load():
            try:
                res = splus("getStickerSet", name=STICKER_SET).get("result") or {}
                return [{"file_id": s["file_id"], "emoji": s.get("emoji", "")}
                        for s in res.get("stickers", [])]
            except Exception as e:  # noqa
                print("Sticker set error:", repr(e))
                return []
        pool += cached("sticker_set", STICKER_TTL, load)
    return pool


def send_sticker(chat: int, mood: str):
    """استیکر مناسب حالت را می‌فرستد؛ اگر استیکری نبود یا خطا شد بی‌صدا رد می‌شود."""
    try:
        pool = sticker_pool()
        if not pool:
            return
        want = {_clean(e) for e in MOODS.get(mood, [])}
        pick = [s for s in pool if _clean(s.get("emoji")) in want] or pool
        splus("sendSticker", chat_id=chat, sticker=random.choice(pick)["file_id"])
    except Exception as e:  # noqa
        print("Sticker error:", repr(e))


# ------------------------- کیبوردها -------------------------
def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


BACK_MENU = [btn("🏠 منوی اصلی", "menu")]


def team_grid(table: list, prefix: str, exclude: int = None) -> list:
    buttons = [btn(f"⚽ {fa_name(r)}", f"{prefix}:{r['team']['id']}")
               for r in sorted(table, key=lambda r: r.get("rank") or 99)
               if r["team"]["id"] != exclude]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


# ------------------------- صفحه‌ها -------------------------
def view_menu():
    text = (f"🏟 <b>بات تحلیل لیگ برتر انگلیس</b>\n{LINE}\n\n"
            "⚽ بازی دلخواهت رو انتخاب کن تا با آمار فصل، فرم تیم‌ها و مدل پیش‌بینی، "
            "تحلیلش رو برات بسازم 📊\n\n👇 یکی از گزینه‌ها رو بزن")
    return text, kb([
        [btn("⚽ تحلیل بازی ⚽", "an")],
        [btn("📊 جدول لیگ", "tab"), btn("🏆 تیم‌ها", "ts")],
        [btn("💡 راهنما", "help"), btn("🆔 شناسه من", "myid")],
    ])


def view_pick_home(table):
    text = f"🏠 <b>تیم میزبان</b>\n{LINE}\n\nتیم میزبان بازی رو انتخاب کن 👇"
    return text, kb(team_grid(table, "h") + [BACK_MENU])


def view_pick_away(table, hid):
    home = team_row(table, hid)
    if not home:
        return view_pick_home(table)
    text = (f"✈️ <b>تیم مهمان</b>\n{LINE}\n\n🏠 میزبان: <b>{esc(fa_name(home))}</b>\n\n"
            "حالا تیم مهمان رو انتخاب کن 👇")
    return text, kb(team_grid(table, f"r{hid}", exclude=hid)
                    + [[btn("🔙 تغییر میزبان", "an"), btn("🏠 منو", "menu")]])


def view_table(table):
    rows = []
    for r in sorted(table, key=lambda r: r.get("rank") or 99):
        h, a = r["home"], r["away"]
        played = h["played"] + a["played"]
        w, d, l = h["win"] + a["win"], h["draw"] + a["draw"], h["lose"] + a["lose"]
        gd = r.get("goalsDiff")
        if gd is None:
            gd = (h["goals"]["for"] + a["goals"]["for"]) - (h["goals"]["against"] + a["goals"]["against"])
        pts = r.get("points", w * 3 + d)
        name = r["team"]["name"][:13]
        rows.append(f"{r.get('rank', ''):>2} {name:<13} {played:>2} {w:>2} {d:>2} {l:>2} {gd:>3} {pts:>3}")
    head = " # Team           P  W  D  L  GD Pts"
    text = f"📊 <b>جدول لیگ برتر انگلیس</b>\n{LINE}\n\n<pre>{esc(head)}\n" + esc("\n".join(rows)) + "</pre>"
    return text, kb([[btn("🔄 بروزرسانی", "tab"), btn("⚽ تحلیل بازی", "an")], BACK_MENU])


def view_teams(table):
    text = f"🏆 <b>تیم‌های لیگ برتر</b>\n{LINE}\n\nیه تیم رو انتخاب کن تا وضعیتش رو ببینی 👇"
    return text, kb(team_grid(table, "t") + [BACK_MENU])


def view_team(table, tid):
    r = team_row(table, tid)
    if not r:
        return view_teams(table)
    h, a = r["home"], r["away"]
    played = h["played"] + a["played"]
    w, d, l = h["win"] + a["win"], h["draw"] + a["draw"], h["lose"] + a["lose"]
    gf = h["goals"]["for"] + a["goals"]["for"]
    ga = h["goals"]["against"] + a["goals"]["against"]
    text = (
        f"⚽ <b>{esc(fa_name(r))}</b>  <i>({esc(r['team']['name'])})</i>\n{LINE}\n\n"
        f"🏅 رتبه: <b>{r.get('rank', '؟')}</b>   ⭐️ امتیاز: <b>{r.get('points', w * 3 + d)}</b>\n"
        f"🎮 بازی: {played}  |  ✅ {w} برد  ➖ {d} مساوی  ❌ {l} باخت\n"
        f"🥅 گل زده: {gf}  |  گل خورده: {ga}\n\n"
        f"🔥 <b>فرم ۵ بازی اخیر</b>\n{form_text(r.get('form'))}\n\n"
        f"🏟 <b>در خانه:</b> {h['win']} برد، {h['draw']} مساوی، {h['lose']} باخت "
        f"({h['goals']['for']}/{h['goals']['against']})\n"
        f"✈️ <b>بیرون:</b> {a['win']} برد، {a['draw']} مساوی، {a['lose']} باخت "
        f"({a['goals']['for']}/{a['goals']['against']})"
    )
    return text, kb([[btn("⚽ تحلیل بازی با این تیم (میزبان)", f"h:{tid}")],
                     [btn("🔙 تیم‌ها", "ts"), btn("🏠 منو", "menu")]])


def view_help():
    text = (f"💡 <b>راهنما</b>\n{LINE}\n\n"
            "1️⃣ روی «⚽ تحلیل بازی» بزن\n"
            "2️⃣ تیم میزبان رو انتخاب کن 🏠\n"
            "3️⃣ تیم مهمان رو انتخاب کن ✈️\n"
            "4️⃣ تحلیل کامل آماده‌ست! 📈\n\n"
            "🎯 مدل پیش‌بینی بر پایه‌ی آمار خانه/بیرون، فرم اخیر و توزیع پواسون ساخته شده.\n\n"
            "⚠️ <i>این فقط یک پیش‌بینی آماریه، نه تضمین. برای شرط‌بندی استفاده نکن.</i>")
    return text, kb([BACK_MENU])


def view_myid(user_id):
    text = (f"🆔 <b>شناسه‌ی عددی تو</b>\n{LINE}\n\n<code>{user_id}</code>\n\n"
            "<i>(برای ادمین شدن در بات به کار می‌آید)</i>")
    return text, kb([BACK_MENU])


def view_error(msg: str):
    return f"😕 {esc(msg)}", kb([[btn("🔄 دوباره", "an"), btn("🏠 منو", "menu")]])


# ------------------------- ساخت تحلیل -------------------------
def build_reply(home: dict, away: dict, table: list) -> str:
    hn, an = esc(fa_name(home)), esc(fa_name(away))
    p = predict(home, away, table)
    h2h = fetch_h2h(home["team"]["id"], away["team"]["id"])
    hh, aa = home["home"], away["away"]

    lines = [
        f"⚽ <b>{hn}</b> 🆚 <b>{an}</b>", LINE, "",
        "🔥 <b>فرم ۵ بازی اخیر</b>",
        f"🏠 {hn}: {form_text(home.get('form'))}",
        f"✈️ {an}: {form_text(away.get('form'))}",
        "",
        f"🏟 <b>{hn}</b> در خانه: {hh['win']} برد، {hh['draw']} مساوی، {hh['lose']} باخت "
        f"(⚽ {hh['goals']['for']}/{hh['goals']['against']})",
        f"✈️ <b>{an}</b> بیرون: {aa['win']} برد، {aa['draw']} مساوی، {aa['lose']} باخت "
        f"(⚽ {aa['goals']['for']}/{aa['goals']['against']})",
    ]

    if h2h:
        hid = home["team"]["id"]
        w = d = l = 0
        recent = []
        for f in h2h:
            gh, ga = f["goals"]["home"], f["goals"]["away"]
            fh = f["teams"]["home"]["id"] == hid
            mine, theirs = (gh, ga) if fh else (ga, gh)
            w, d, l = w + (mine > theirs), d + (mine == theirs), l + (mine < theirs)
            recent.append(f"{f['teams']['home']['name']} {gh}-{ga} {f['teams']['away']['name']}")
        lines += ["", f"🤝 <b>رودررو</b> (آخرین {len(h2h)} بازی): {hn} {w} برد، {d} مساوی، {l} باخت"]
        lines += [f"▫️ {esc(x)}" for x in recent[:3]]

    lines += [
        "", f"🎯 <b>گل مورد انتظار</b>",
        f"{hn} <b>{p['lam_h']:.2f}</b>  ⚽  {an} <b>{p['lam_a']:.2f}</b>",
        "", "📈 <b>احتمال نتیجه</b>",
        f"🏠 برد {hn}: <b>{p['home'] * 100:.0f}%</b>\n{bar(p['home'])}",
        f"🤝 مساوی: <b>{p['draw'] * 100:.0f}%</b>\n{bar(p['draw'])}",
        f"✈️ برد {an}: <b>{p['away'] * 100:.0f}%</b>\n{bar(p['away'])}",
        "", "🔢 <b>محتمل‌ترین نتایج</b>",
    ]
    lines += [f"{MEDALS[k]} {i}-{j}  ({pr * 100:.0f}%)" for k, (pr, i, j) in enumerate(p["scores"])]
    lines += [
        "",
        f"🔥 بیش از ۲.۵ گل: <b>{p['over25'] * 100:.0f}%</b>",
        f"🥅 هر دو تیم گل می‌زنند: <b>{p['btts'] * 100:.0f}%</b>",
        "", LINE,
        "⚠️ <i>پیش‌بینی آماری است و تضمینی نیست؛ برای شرط‌بندی استفاده نکنید.</i>",
    ]
    return "\n".join(lines), p


def result_markup(hid: int, aid: int) -> dict:
    return kb([
        [btn("🔁 جابجایی میزبان و مهمان", f"r{aid}:{hid}")],
        [btn("⚽ تحلیل جدید", "an"), btn("📊 جدول", "tab")],
        BACK_MENU,
    ])


# ------------------------- ارتباط با سروش پلاس -------------------------
def splus(method: str, **params):
    url = SPLUS_BASE.format(token=BOT_TOKEN) + "/" + method
    r = requests.post(url, json=params, timeout=70)
    try:
        data = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if not data.get("ok"):
        wait = (data.get("parameters") or {}).get("retry_after")
        if wait:
            time.sleep(int(wait))
        raise RuntimeError(f"Soroush error {data.get('error_code')}: {data.get('description')}")
    return data


def send(chat: int, text: str, markup: dict = None):
    params = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML"}
    if markup:
        params["reply_markup"] = markup
    return splus("sendMessage", **params)


def show(chat: int, mid: int, view):
    """صفحه را روی همان پیام ویرایش می‌کند (اگر نشد، پیام تازه می‌فرستد)."""
    text, markup = view
    try:
        splus("editMessageText", chat_id=chat, message_id=mid, text=text[:4096],
              parse_mode="HTML", reply_markup=markup)
    except Exception as e:  # noqa
        if "not modified" in str(e).lower():
            return
        print("Edit failed:", repr(e))
        send(chat, text, markup)


def api_error_text(e: Exception) -> str:
    if isinstance(e, requests.HTTPError):
        return f"خطا در دریافت اطلاعات از API ({e.response.status_code}). کمی بعد دوباره امتحان کن."
    print("ERROR:", repr(e))
    return "مشکلی پیش آمد. دوباره امتحان کن."


def deliver_analysis(chat: int, mid, home_id: int, away_id: int):
    """ساخت تحلیل: پیام انتخاب پاک می‌شود، استیکر و بعد نتیجه ارسال می‌شود."""
    if mid:
        show(chat, mid, ("⏳ <b>در حال تحلیل بازی...</b>\n\n📡 آمار و رودررو رو می‌خونم، یه لحظه صبر کن 🙏", None))
    try:
        table = get_table()
        home, away = team_row(table, home_id), team_row(table, away_id)
        if not home or not away or home_id == away_id:
            raise ValueError("تیم‌ها پیدا نشدند")
        text, p = build_reply(home, away, table)
    except Exception as e:  # noqa
        view = view_error(api_error_text(e))
        if mid:
            show(chat, mid, view)
        else:
            send(chat, *view)
        send_sticker(chat, "sad")
        return
    if mid:
        try:
            splus("deleteMessage", chat_id=chat, message_id=mid)
        except Exception:  # noqa
            pass
    top = max(p["home"], p["draw"], p["away"])
    send_sticker(chat, "hot" if top >= 0.5 else "think")
    send(chat, text, result_markup(home_id, away_id))


# ------------------------- مسیریابی -------------------------
def on_callback(cq: dict):
    try:
        splus("answerCallbackQuery", callback_query_id=cq["id"])
    except Exception:  # noqa
        pass
    msg = cq.get("message") or {}
    chat, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    data = cq.get("data") or ""
    if not chat or not mid:
        return

    if data == "menu":
        return show(chat, mid, view_menu())
    if data == "help":
        return show(chat, mid, view_help())
    if data == "myid":
        return show(chat, mid, view_myid((cq.get("from") or {}).get("id", "؟")))

    if data.startswith("r") and ":" in data and data[1:].split(":")[0].isdigit():
        hid, aid = (int(x) for x in data[1:].split(":"))
        return deliver_analysis(chat, mid, hid, aid)

    try:
        table = get_table()
        if not table:
            return show(chat, mid, view_error("اطلاعات جدول لیگ در دسترس نیست."))
        if data == "an":
            return show(chat, mid, view_pick_home(table))
        if data.startswith("h:"):
            return show(chat, mid, view_pick_away(table, int(data[2:])))
        if data == "tab":
            return show(chat, mid, view_table(table))
        if data == "ts":
            return show(chat, mid, view_teams(table))
        if data.startswith("t:"):
            return show(chat, mid, view_team(table, int(data[2:])))
    except Exception as e:  # noqa
        return show(chat, mid, view_error(api_error_text(e)))
    show(chat, mid, view_menu())


def parse_typed(text: str):
    parts = re.split(r"\s*(?:\bvs\.?\b|[-–—])\s*", text.strip(), maxsplit=1, flags=re.I)
    return parts if len(parts) == 2 and all(p.strip() for p in parts) else None


def on_message(msg: dict):
    chat = (msg.get("chat") or {}).get("id")
    if not chat:
        return
    uid = str((msg.get("from") or {}).get("id", ""))

    st = msg.get("sticker")
    if st:  # ادمین استیکر بفرستد، برای بات ذخیره می‌شود
        if ADMIN_ID and uid == ADMIN_ID:
            n = save_sticker(st["file_id"], st.get("emoji", ""))
            send(chat, f"✅ استیکر ذخیره شد {esc(st.get('emoji', ''))}\n🎭 تعداد استیکرها: <b>{n}</b>",
                 kb([BACK_MENU]))
        else:
            send(chat, *view_menu())
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start"):
        send_sticker(chat, "welcome")
        return send(chat, *view_menu())

    # حالت مخفی: تایپ مستقیم «Arsenal - Chelsea» هم کار می‌کند
    pair = None if text.startswith("/") else parse_typed(text)
    if pair:
        try:
            table = get_table()
            home, away = find_team(pair[0], table), find_team(pair[1], table)
            if home and away and home is not away:
                return deliver_analysis(chat, None, home["team"]["id"], away["team"]["id"])
        except Exception as e:  # noqa
            return send(chat, *view_error(api_error_text(e)))
    send(chat, "👇 برای ادامه از دکمه‌های زیر استفاده کن", None)
    send(chat, *view_menu())


def main():
    if not BOT_TOKEN or not VICTORY_KEY:
        raise SystemExit("BOT_TOKEN و VICTORY_API_KEY را در متغیرهای محیطی بگذارید.")
    try:  # فقط /start در منوی دستورات بماند
        splus("setMyCommands", commands=[{"command": "start", "description": "شروع بات 🚀"}])
    except Exception as e:  # noqa
        print("setMyCommands failed:", repr(e))
    offset = None
    print("Bot started.")
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            data = splus("getUpdates", **params)
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if upd.get("callback_query"):
                        on_callback(upd["callback_query"])
                    elif upd.get("message"):
                        on_message(upd["message"])
                except Exception as e:  # noqa
                    print("Update error:", repr(e))
        except Exception as e:  # noqa
            print("Loop error:", repr(e))
            time.sleep(3)


if __name__ == "__main__":
    main()
