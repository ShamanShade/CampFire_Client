"""
Emoji for Campfire: the picker's categories, :shortcode: names, and helpers.

Search and most shortcodes use the official Unicode names (Python's unicodedata), so
:grinning_face: or :pizza: work without a big table. ALIASES adds the common short ones.
"""

from __future__ import annotations

import re
import unicodedata

CATEGORIES = {
    "Smileys": (
        "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 🙃 🫠 😉 😊 😇 🥰 😍 🤩 😘 😗 ☺️ 😚 😙 🥲 😋 😛 😜 🤪 😝 🤑 🤗 🤭 🫢 "
        "🫣 🤫 🤔 🫡 🤐 🤨 😐 😑 😶 🫥 😏 😒 🙄 😬 😮‍💨 🤥 😌 😔 😪 🤤 😴 😷 🤒 🤕 🤢 🤮 🤧 🥵 🥶 🥴 😵 "
        "😵‍💫 🤯 🤠 🥳 🥸 😎 🤓 🧐 😕 🫤 😟 🙁 ☹️ 😮 😯 😲 😳 🥺 🥹 😦 😧 😨 😰 😥 😢 😭 😱 😖 😣 😞 😓 "
        "😩 😫 🥱 😤 😡 😠 🤬 😈 👿 💀 ☠️ 💩 🤡 👹 👺 👻 👽 👾 🤖 😺 😸 😹 😻 😼 😽 🙀 😿 😾"
    ),
    "People": (
        "👋 🤚 🖐️ ✋ 🖖 🫱 🫲 👌 🤌 🤏 ✌️ 🤞 🫰 🤟 🤘 🤙 👈 👉 👆 🖕 👇 ☝️ 🫵 👍 👎 ✊ 👊 🤛 🤜 👏 🙌 🫶 "
        "👐 🤲 🤝 🙏 ✍️ 💅 🤳 💪 🦾 🧠 🫀 👀 👁️ 👅 👄 🫦 👶 🧒 👦 👧 🧑 👱 👨 🧔 👩 🧓 👴 👵 🙍 🙎 🙅 🙆 "
        "💁 🙋 🧏 🙇 🤦 🤷 👮 🕵️ 💂 🥷 👷 🤴 👸 👳 👲 🧕 🤵 👰 🤰 🫄 👼 🎅 🤶 🦸 🦹 🧙 🧚 🧛 🧜 🧝 🧞 🧟 "
        "💆 💇 🚶 🧍 🧎 🏃 💃 🕺 👯 🧖 🧗 🏇 ⛷️ 🏂 🏌️ 🏄 🚣 🏊 ⛹️ 🏋️ 🚴 🚵 🤸 🤼 🤽 🤾 🤹 🧘 🛀 👭 👫 👬 💏 💑 👪"
    ),
    "Animals": (
        "🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐻‍❄️ 🐨 🐯 🦁 🐮 🐷 🐽 🐸 🐵 🙈 🙉 🙊 🐒 🐔 🐧 🐦 🐤 🐣 🐥 🦆 🦅 🦉 🦇 "
        "🐺 🐗 🐴 🦄 🐝 🪱 🐛 🦋 🐌 🐞 🐜 🪰 🪲 🪳 🦟 🦗 🕷️ 🦂 🐢 🐍 🦎 🦖 🦕 🐙 🦑 🦐 🦞 🦀 🐡 🐠 🐟 🐬 "
        "🐳 🐋 🦈 🦭 🐊 🐅 🐆 🦓 🦍 🦧 🦣 🐘 🦛 🦏 🐪 🐫 🦒 🦘 🦬 🐃 🐂 🐄 🐎 🐖 🐏 🐑 🦙 🐐 🦌 🐕 🐩 🦮 "
        "🐈 🐈‍⬛ 🪶 🐓 🦃 🦤 🦚 🦜 🦢 🦩 🕊️ 🐇 🦝 🦨 🦡 🦫 🦦 🦥 🐁 🐀 🐿️ 🦔 🐾 🐉 🐲 🌵 🎄 🌲 🌳 🌴 🪵 🌱 "
        "🌿 ☘️ 🍀 🎍 🪴 🎋 🍃 🍂 🍁 🍄 🐚 🪨 🌾 💐 🌷 🌹 🥀 🌺 🌸 🌼 🌻"
    ),
    "Food": (
        "🍏 🍎 🍐 🍊 🍋 🍌 🍉 🍇 🍓 🫐 🍈 🍒 🍑 🥭 🍍 🥥 🥝 🍅 🍆 🥑 🥦 🥬 🥒 🌶️ 🫑 🌽 🥕 🫒 🧄 🧅 🥔 🍠 "
        "🥐 🥯 🍞 🥖 🥨 🧀 🥚 🍳 🧈 🥞 🧇 🥓 🥩 🍗 🍖 🌭 🍔 🍟 🍕 🫓 🥪 🥙 🧆 🌮 🌯 🫔 🥗 🥘 🫕 🥫 🍝 🍜 "
        "🍲 🍛 🍣 🍱 🥟 🦪 🍤 🍙 🍚 🍘 🍥 🥠 🥮 🍢 🍡 🍧 🍨 🍦 🥧 🧁 🍰 🎂 🍮 🍭 🍬 🍫 🍿 🍩 🍪 🌰 🥜 🍯 "
        "🥛 🍼 ☕ 🫖 🍵 🧃 🥤 🧋 🍶 🍺 🍻 🥂 🍷 🥃 🍸 🍹 🧉 🍾 🧊 🥄 🍴 🍽️"
    ),
    "Activities": (
        "⚽ 🏀 🏈 ⚾ 🥎 🎾 🏐 🏉 🥏 🎱 🪀 🏓 🏸 🏒 🏑 🥍 🏏 🪃 🥅 ⛳ 🪁 🏹 🎣 🤿 🥊 🥋 🎽 🛹 🛼 🛷 ⛸️ 🥌 "
        "🎿 🏆 🥇 🥈 🥉 🏅 🎖️ 🎗️ 🎫 🎟️ 🎪 🎭 🎨 🎬 🎤 🎧 🎼 🎹 🥁 🪘 🎷 🎺 🪗 🎸 🪕 🎻 🎲 ♟️ 🎯 🎳 🎮 🕹️ "
        "🎰 🧩 🎉 🎊 🎈 🎁 🎀 🪅 🪩 🎃 🎆 🎇 🧨 ✨"
    ),
    "Travel": (
        "🚗 🚕 🚙 🚌 🚎 🏎️ 🚓 🚑 🚒 🚐 🛻 🚚 🚛 🚜 🏍️ 🛵 🚲 🛴 🚨 🚔 🚍 🚘 🚖 ✈️ 🛫 🛬 🚀 🛸 🚁 🛶 ⛵ 🚤 "
        "🛳️ ⛴️ 🚢 ⚓ 🚧 ⛽ 🚏 🗺️ 🗿 🗽 🗼 🏰 🏯 🏟️ 🎡 🎢 🎠 ⛲ ⛱️ 🏖️ 🏝️ 🏜️ 🌋 ⛰️ 🏔️ 🗻 🏕️ ⛺ 🏠 🏡 🏘️ 🏢 "
        "🏬 🏥 🏦 🏨 🏪 🏫 🏛️ ⛪ 🕌 🕍 🌅 🌄 🌠 🎑 🏙️ 🌃 🌌 🌉 🌁 🌍 🌎 🌏 🌐 🌑 🌒 🌓 🌔 🌕 🌙 ⭐ 🌟 ☀️ "
        "🌤️ ⛅ ☁️ 🌧️ ⛈️ 🌩️ 🌨️ ❄️ ☃️ ⛄ 🌬️ 💨 🌪️ 🌈 ☔ ⚡ 🔥 💧 🌊"
    ),
    "Objects": (
        "⌚ 📱 💻 ⌨️ 🖥️ 🖨️ 🖱️ 💽 💾 💿 📀 📷 📸 📹 🎥 📞 ☎️ 📺 📻 🎙️ ⏰ ⌛ ⏳ 📡 🔋 🔌 💡 🔦 🕯️ 🧯 💸 💵 "
        "💰 💳 💎 ⚖️ 🧰 🔧 🔨 ⚒️ 🛠️ ⛏️ 🔩 ⚙️ 🧱 🔫 💣 🔪 🗡️ ⚔️ 🛡️ 🚬 ⚰️ 🔮 📿 🧿 💈 ⚗️ 🔭 🔬 🩹 🩺 💊 💉 "
        "🧬 🦠 🧪 🌡️ 🧹 🧺 🧻 🚽 🛁 🧼 🪥 🧽 🛎️ 🔑 🗝️ 🚪 🪑 🛋️ 🛏️ 🧸 🖼️ 🪞 🛍️ 🛒 🎁 ✉️ 📩 📨 📧 💌 📦 🏷️ "
        "📪 📬 📮 📜 📃 📄 📑 📊 📈 📉 🗒️ 🗓️ 📆 📅 🗑️ 📇 🗃️ 🗳️ 🗄️ 📋 📁 📂 🗂️ 🗞️ 📰 📓 📔 📒 📕 📗 📘 📙 "
        "📚 📖 🔖 🔗 📎 🖇️ 📐 📏 📌 📍 ✂️ 🖊️ 🖋️ ✒️ 🖌️ 🖍️ 📝 ✏️ 🔍 🔎 🔏 🔐 🔒 🔓"
    ),
    "Symbols": (
        "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❤️‍🔥 ❤️‍🩹 ❣️ 💕 💞 💓 💗 💖 💘 💝 💟 ☮️ ✝️ ☪️ 🕉️ ☸️ ✡️ 🔯 ☯️ ☦️ 🛐 "
        "⛎ ♈ ♉ ♊ ♋ ♌ ♍ ♎ ♏ ♐ ♑ ♒ ♓ 🆔 ⚛️ ☢️ ☣️ 📴 📳 🈶 🆚 💮 🉐 ㊙️ ㊗️ 🅰️ 🅱️ 🆎 🆑 🅾️ 🆘 ❌ ⭕ 🛑 ⛔ "
        "📛 🚫 💯 💢 ♨️ 🚷 🚯 🚳 🚱 🔞 📵 🚭 ❗ ❕ ❓ ❔ ‼️ ⁉️ 🔅 🔆 〽️ ⚠️ 🚸 🔱 ⚜️ 🔰 ♻️ ✅ 💹 ❇️ ✳️ ❎ 🌐 "
        "💠 Ⓜ️ 🌀 💤 🏧 🚾 ♿ 🅿️ 🚹 🚺 🚼 🚻 🚮 🎦 📶 🆗 🆙 🆒 🆕 🆓 0️⃣ 1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣ 9️⃣ 🔟 "
        "🔢 #️⃣ *️⃣ ▶️ ⏸️ ⏯️ ⏹️ ⏺️ ⏭️ ⏮️ ⏩ ⏪ 🔀 🔁 🔂 ◀️ 🔼 🔽 ➡️ ⬅️ ⬆️ ⬇️ ↗️ ↘️ ↙️ ↖️ ↕️ ↔️ 🔄 ➕ ➖ ➗ "
        "✖️ ♾️ 💲 💱 ™️ ©️ ®️ 〰️ ➰ ➿ 🔚 🔙 🔛 🔝 🔜 ✔️ ☑️ 🔘 🔴 🟠 🟡 🟢 🔵 🟣 ⚫ ⚪ 🟤 🔺 🔻 🔸 🔹 🔶 🔷 "
        "🔳 🔲 ▪️ ▫️ ◾ ◽ ◼️ ◻️ 🟥 🟧 🟨 🟩 🟦 🟪 ⬛ ⬜ 🟫 🔈 🔇 🔉 🔊 🔔 🔕 📣 📢 💬 💭 🗯️ ♠️ ♣️ ♥️ ♦️ 🃏 🎴 "
        "🀄 🕐 🕑 🕒 🕓 🕔 🕕 🕖 🕗 🕘 🕙 🕚 🕛"
    ),
    "Flags": (
        "🏁 🚩 🎌 🏴 🏳️ 🏳️‍🌈 🏳️‍⚧️ 🏴‍☠️ 🇺🇸 🇨🇦 🇲🇽 🇧🇷 🇦🇷 🇬🇧 🇮🇪 🇫🇷 🇩🇪 🇪🇸 🇵🇹 🇮🇹 🇳🇱 🇧🇪 🇨🇭 🇦🇹 🇸🇪 🇳🇴 🇩🇰 🇫🇮 "
        "🇵🇱 🇺🇦 🇷🇺 🇹🇷 🇬🇷 🇮🇱 🇪🇬 🇿🇦 🇳🇬 🇰🇪 🇮🇳 🇵🇰 🇨🇳 🇯🇵 🇰🇷 🇹🇼 🇭🇰 🇸🇬 🇵🇭 🇻🇳 🇹🇭 🇮🇩 🇦🇺 🇳🇿 🇸🇦 🇦🇪 🇯🇲 "
        "🇵🇷 🇨🇴 🇨🇱 🇵🇪"
    ),
}
CATEGORIES = {k: v.split() for k, v in CATEGORIES.items()}
CATEGORY_ICONS = {"Smileys": "😀", "People": "👋", "Animals": "🐶", "Food": "🍔", "Activities": "⚽",
                  "Travel": "🚗", "Objects": "💡", "Symbols": "❤️", "Flags": "🏁"}

# common short names people type (anything else can use its Unicode name, e.g. :rolling_on_the_floor_laughing:)
ALIASES = {
    "smile": "😄", "smiley": "😃", "grin": "😁", "grinning": "😀", "laughing": "😆", "joy": "😂", "rofl": "🤣",
    "sweat_smile": "😅", "slight_smile": "🙂", "upside_down": "🙃", "wink": "😉", "blush": "😊", "innocent": "😇",
    "heart_eyes": "😍", "star_struck": "🤩", "kissing_heart": "😘", "yum": "😋", "tongue": "😛",
    "stuck_out_tongue_winking_eye": "😜", "zany": "🤪", "money_mouth": "🤑", "hugs": "🤗", "hug": "🤗",
    "shush": "🤫", "thinking": "🤔", "think": "🤔", "neutral": "😐", "expressionless": "😑", "no_mouth": "😶",
    "smirk": "😏", "unamused": "😒", "eye_roll": "🙄", "roll_eyes": "🙄", "grimace": "😬", "relieved": "😌",
    "pensive": "😔", "sleepy": "😪", "drool": "🤤", "sleeping": "😴", "mask": "😷", "sick": "🤢", "vomit": "🤮",
    "sneeze": "🤧", "hot": "🥵", "cold": "🥶", "woozy": "🥴", "dizzy_face": "😵", "mind_blown": "🤯",
    "exploding_head": "🤯", "cowboy": "🤠", "party": "🥳", "partying_face": "🥳", "sunglasses": "😎", "cool": "😎",
    "nerd": "🤓", "monocle": "🧐", "confused": "😕", "worried": "😟", "frown": "🙁", "open_mouth": "😮",
    "astonished": "😲", "flushed": "😳", "pleading": "🥺", "puppy_eyes": "🥺", "fearful": "😨", "cold_sweat": "😰",
    "sad": "😢", "cry": "😢", "sob": "😭", "scream": "😱", "confounded": "😖", "persevere": "😣",
    "disappointed": "😞", "sweat": "😓", "weary": "😩", "tired": "😫", "yawn": "🥱", "triumph": "😤",
    "angry": "😠", "rage": "😡", "cursing": "🤬", "imp": "😈", "skull": "💀", "poop": "💩", "clown": "🤡",
    "ghost": "👻", "alien": "👽", "robot": "🤖", "smiley_cat": "😺", "heart_eyes_cat": "😻",
    "wave": "👋", "ok_hand": "👌", "ok": "👌", "pinch": "🤌", "v": "✌️", "peace": "✌️", "crossed_fingers": "🤞",
    "love_you": "🤟", "metal": "🤘", "call_me": "🤙", "point_left": "👈", "point_right": "👉", "point_up": "☝️",
    "point_down": "👇", "middle_finger": "🖕", "+1": "👍", "thumbsup": "👍", "thumbs_up": "👍", "-1": "👎",
    "thumbsdown": "👎", "thumbs_down": "👎", "fist": "✊", "punch": "👊", "clap": "👏", "raised_hands": "🙌",
    "heart_hands": "🫶", "handshake": "🤝", "pray": "🙏", "please": "🙏", "muscle": "💪", "flex": "💪",
    "eyes": "👀", "brain": "🧠", "facepalm": "🤦", "shrug": "🤷", "dancer": "💃", "dance": "🕺",
    "heart": "❤️", "red_heart": "❤️", "orange_heart": "🧡", "yellow_heart": "💛", "green_heart": "💚",
    "blue_heart": "💙", "purple_heart": "💜", "black_heart": "🖤", "white_heart": "🤍", "broken_heart": "💔",
    "sparkling_heart": "💖", "two_hearts": "💕", "fire": "🔥", "flame": "🔥", "campfire": "🔥", "100": "💯",
    "boom": "💥", "sparkles": "✨", "star": "⭐", "star2": "🌟", "zap": "⚡", "rainbow": "🌈", "sun": "☀️",
    "moon": "🌙", "snowflake": "❄️", "cloud": "☁️", "umbrella": "☔", "check": "✅", "white_check_mark": "✅",
    "x": "❌", "warning": "⚠️", "question": "❓", "exclamation": "❗", "zzz": "💤", "speech": "💬",
    "thought": "💭", "tada": "🎉", "party_popper": "🎉", "confetti": "🎊", "balloon": "🎈", "gift": "🎁",
    "trophy": "🏆", "medal": "🏅", "crown": "👑", "gem": "💎", "money": "💰", "dollar": "💵", "gg": "🎮",
    "video_game": "🎮", "game": "🎮", "joystick": "🕹️", "dice": "🎲", "music": "🎵", "notes": "🎶",
    "headphones": "🎧", "mic": "🎤", "guitar": "🎸", "camera": "📷", "tv": "📺", "computer": "💻",
    "phone": "📱", "bulb": "💡", "lock": "🔒", "key": "🔑", "bell": "🔔", "pin": "📌", "link": "🔗",
    "pizza": "🍕", "burger": "🍔", "hamburger": "🍔", "fries": "🍟", "hotdog": "🌭", "taco": "🌮", "cake": "🎂",
    "cookie": "🍪", "donut": "🍩", "coffee": "☕", "tea": "🍵", "beer": "🍺", "beers": "🍻", "wine": "🍷",
    "cheers": "🥂", "popcorn": "🍿", "apple": "🍎", "banana": "🍌", "avocado": "🥑", "dog": "🐶", "cat": "🐱",
    "fox": "🦊", "bear": "🐻", "panda": "🐼", "lion": "🦁", "frog": "🐸", "monkey": "🐵", "see_no_evil": "🙈",
    "hear_no_evil": "🙉", "speak_no_evil": "🙊", "chicken": "🐔", "penguin": "🐧", "unicorn": "🦄", "bee": "🐝",
    "butterfly": "🦋", "snake": "🐍", "dragon": "🐉", "shark": "🦈", "whale": "🐳", "dolphin": "🐬",
    "turtle": "🐢", "rocket": "🚀", "car": "🚗", "plane": "✈️", "earth": "🌍", "tree": "🌳", "cactus": "🌵",
    "rose": "🌹", "sunflower": "🌻", "clover": "🍀", "leaves": "🍃", "mushroom": "🍄", "wood": "🪵",
    "tent": "⛺", "camping": "🏕️", "mountain": "⛰️", "beach": "🏖️", "ocean": "🌊", "salute": "🫡",
    "melting": "🫠", "skull_crossbones": "☠️", "nail_care": "💅", "lol": "😂", "lmao": "🤣",
}

SHORTCODE_RE = re.compile(r":([a-z0-9_+\-]{1,40}):")


def emoji_for(code: str) -> str | None:
    """:code: -> emoji, using ALIASES first, then the Unicode name ("grinning_face" -> 😀)."""
    code = code.lower()
    if code in ALIASES:
        return ALIASES[code]
    try:
        ch = unicodedata.lookup(code.replace("_", " ").replace("-", " ").upper())
    except KeyError:
        return None
    return ch if is_emoji_char(ch) else None


def replace_shortcodes(text: str) -> str:
    return SHORTCODE_RE.sub(lambda m: emoji_for(m.group(1)) or m.group(0), text)


def is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF
            or 0x2190 <= cp <= 0x21FF or 0x2300 <= cp <= 0x23FF or 0x25A0 <= cp <= 0x25FF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299))


_JOINERS = {0xFE0F, 0xFE0E, 0x200D, 0x20E3} | set(range(0x1F3FB, 0x1F400)) | set(range(0xE0020, 0xE0080))


def only_emoji(text: str, limit=10) -> bool:
    """True for short messages made only of emoji (they're shown big)."""
    text = "".join(text.split())
    if not text:
        return False
    count, i = 0, 0
    while i < len(text):
        ch, cp = text[i], ord(text[i])
        nxt = ord(text[i + 1]) if i + 1 < len(text) else 0
        if cp in _JOINERS:
            pass
        elif ch in "#*0123456789":                       # keycaps like 1️⃣ only
            if nxt not in (0xFE0F, 0x20E3):
                return False
            count += 1
        elif not is_emoji_char(ch):
            return False
        elif 0x1F1E6 <= cp <= 0x1F1FF:                   # a flag is two regional letters
            if 0x1F1E6 <= nxt <= 0x1F1FF:
                i += 1
            count += 1
        else:
            count += 1
        i += 1
    return 0 < count <= limit


def name_of(emoji: str) -> str:
    """Searchable name for an emoji ("grinning face"), from its first meaningful character."""
    names = []
    for ch in emoji:
        if ord(ch) in _JOINERS:
            continue
        try:
            names.append(unicodedata.name(ch).lower())
        except ValueError:
            pass
    return " ".join(names)


def search(query: str, limit=120) -> list[str]:
    q = query.lower().strip().strip(":")
    if not q:
        return []
    out, seen = [], set()
    for alias, e in ALIASES.items():            # aliases first: "lol", "gg", "fire"...
        if q in alias and e not in seen:
            out.append(e)
            seen.add(e)
    for items in CATEGORIES.values():
        for e in items:
            if e not in seen and q in name_of(e):
                out.append(e)
                seen.add(e)
    return out[:limit]


def all_names():
    """{alias or unicode name with underscores: emoji} for the shortcode autocomplete."""
    names = dict(ALIASES)
    for items in CATEGORIES.values():
        for e in items:
            n = name_of(e).split(" ")
            key = "_".join(n)
            names.setdefault(key, e)
    return names
