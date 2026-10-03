import os
import re
import io
import asyncio
import tempfile
import subprocess
from pathlib import Path

from PIL import Image, ImageOps, ImageEnhance
import pytesseract

from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, PicklePersistence, filters
)

TOKEN = os.environ.get("BOT_TOKEN")

# 常用国家：国家名 -> (显示名称, 国际区号)
COUNTRIES = {
    "美国": ("美国", "+1"), "usa": ("美国", "+1"), "us": ("美国", "+1"),
    "英国": ("英国", "+44"), "uk": ("英国", "+44"),
    "菲律宾": ("菲律宾", "+63"), "philippines": ("菲律宾", "+63"),
    "澳大利亚": ("澳大利亚", "+61"), "australia": ("澳大利亚", "+61"),
    "加拿大": ("加拿大", "+1"), "canada": ("加拿大", "+1"),
    "摩洛哥": ("摩洛哥", "+212"), "morocco": ("摩洛哥", "+212"),
}

DEFAULT_FIELDS = ["日期", "编号", "电话", "国家", "金额", "姓名"]
OCR_MAX_SIDE = 1600
ALBUM_WAIT = 1.2
ALBUMS = {}
ALBUM_TASKS = {}


def defaults():
    return {
        "date": "10.4",
        "country": "英国",
        "code": "+44",
        "number": 1,
        "fields": DEFAULT_FIELDS.copy(),
        "values": {"姓名": ""},
    }


def state(context):
    s = context.chat_data
    if "cfg" not in s:
        s["cfg"] = defaults()
    return s["cfg"]


def normalize_code(code):
    d = re.sub(r"\D", "", code)
    return "+" + d if d else ""


def prepare_image(image):
    image = ImageOps.exif_transpose(image).convert("RGB")
    w, h = image.size
    m = max(w, h)
    if m > OCR_MAX_SIDE:
        scale = OCR_MAX_SIDE / m
        image = image.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS
        )
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)
    return ImageEnhance.Contrast(image).enhance(1.2)


def ocr(image):
    return pytesseract.image_to_string(
        prepare_image(image),
        lang="eng",
        config="--oem 3 --psm 6"
    )


def find_phone(text, country_code):
    cc = re.sub(r"\D", "", country_code)

    # 国际号码，允许括号、空格、横线
    candidates = re.findall(
        r"\+\s*\d(?:[\s().\-–—]*\d){7,14}",
        text or ""
    )
    for raw in candidates:
        digits = re.sub(r"\D", "", raw)
        if digits.startswith(cc) and len(digits) - len(cc) >= 7:
            return "+" + digits

    # OCR 偶尔漏掉 +
    loose = re.findall(
        r"(?<!\d)\d(?:[\s().\-–—]*\d){7,14}(?!\d)",
        text or ""
    )
    for raw in loose:
        digits = re.sub(r"\D", "", raw)
        if digits.startswith(cc) and len(digits) - len(cc) >= 7:
            return "+" + digits

    # +1 的本地 10 位号码
    if cc == "1":
        for raw in loose:
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 10:
                return "+1" + digits

    return ""


MONEY = [
    (re.compile(r"\$\s*(\d+(?:[,\s]\d{3})*(?:\.\d{1,2})?)"), "$"),
    (re.compile(r"£\s*(\d+(?:[,\s]\d{3})*(?:\.\d{1,2})?)"), "£"),
    (re.compile(r"€\s*(\d+(?:[,\s]\d{3})*(?:\.\d{1,2})?)"), "€"),
    (re.compile(r"\b(USD|GBP|EUR|AUD|CAD|PHP)\s*[:\-]?\s*(\d+(?:[,\s]\d{3})*(?:\.\d{1,2})?)\b", re.I), None),
]


def find_amount(text):
    for pattern, symbol in MONEY:
        m = pattern.search(text or "")
        if not m:
            continue
        if symbol:
            return symbol + re.sub(r"\s", "", m.group(1))
        return m.group(1).upper() + " " + re.sub(r"\s", "", m.group(2))
    return ""


def caption(cfg, number, phone, amount):
    auto = {
        "日期": cfg["date"],
        "编号": str(number),
        "电话": phone,
        "国家": cfg["country"],
        "金额": amount,
    }
    lines = []
    for field in cfg["fields"]:
        value = auto.get(field, cfg["values"].get(field, ""))
        lines.append(f"{field}：{value}")
    return "\n".join(lines)


def consume_number(cfg):
    n = int(cfg.get("number", 1))
    cfg["number"] = 1 if n >= 1000 else n + 1
    return n


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state(context)
    await update.message.reply_text(
        "机器人已启动。\n"
        "照片/相册/视频都可以直接发送。\n\n"
        "常用命令：\n"
        "/country 菲律宾\n"
        "/date 10.4\n"
        "/number 1\n"
        "/name 张三\n"
        "/add 推荐人\n"
        "/del 推荐人\n"
        "/set 推荐人 李四\n"
        "/settings"
    )


async def country_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = state(context)
    if not context.args:
        await update.message.reply_text("例如：/country 菲律宾")
        return
    raw = " ".join(context.args).strip()
    key = raw.lower()
    if key in COUNTRIES:
        name, code = COUNTRIES[key]
    elif len(context.args) >= 2 and context.args[-1].startswith("+"):
        name = " ".join(context.args[:-1])
        code = normalize_code(context.args[-1])
    else:
        await update.message.reply_text(
            "暂未内置这个国家。可这样设置：/country 新加坡 +65"
        )
        return
    cfg["country"], cfg["code"] = name, code
    await update.message.reply_text(f"已切换：{name}，只优先识别 {code} 电话。")


async def date_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("例如：/date 10.4")
        return
    state(context)["date"] = " ".join(context.args)
    await update.message.reply_text("日期已修改。")


async def number_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("例如：/number 1")
        return
    n = int(context.args[0])
    if not 1 <= n <= 1000:
        await update.message.reply_text("编号请输入 1～1000。")
        return
    state(context)["number"] = n
    await update.message.reply_text(f"下一个编号：{n}")


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = state(context)
    cfg["values"]["姓名"] = " ".join(context.args).strip()
    await update.message.reply_text("姓名已修改。")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = " ".join(context.args).strip()
    if not field:
        await update.message.reply_text("例如：/add 推荐人")
        return
    cfg = state(context)
    if field not in cfg["fields"]:
        cfg["fields"].append(field)
        cfg["values"][field] = ""
    await update.message.reply_text(f"已增加：{field}：")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = " ".join(context.args).strip()
    cfg = state(context)
    if field in cfg["fields"]:
        cfg["fields"].remove(field)
    await update.message.reply_text(f"已删除字段：{field}")


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("例如：/set 推荐人 李四")
        return
    field = context.args[0]
    value = " ".join(context.args[1:])
    cfg = state(context)
    if field not in cfg["fields"]:
        cfg["fields"].append(field)
    cfg["values"][field] = value
    await update.message.reply_text(f"{field} 已设为：{value}")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = state(context)
    preview = caption(cfg, cfg["number"], f"{cfg['code']}…", "")
    await update.message.reply_text(
        f"当前电话区号：{cfg['code']}\n下一个编号：{cfg['number']}\n\n{preview}"
    )


async def analyze_photo(photo):
    f = await photo.get_file()
    data = await f.download_as_bytearray()
    image = Image.open(io.BytesIO(bytes(data)))
    text = await asyncio.to_thread(ocr, image)
    return text


async def process_one_photo(update, context):
    cfg = state(context)
    try:
        photo = update.message.photo[-1]
        text = await analyze_photo(photo)
        phone = find_phone(text, cfg["code"])
        amount = find_amount(text)

        if not phone:
            await update.message.reply_text(
                f"没有识别到 {cfg['code']} 开头的电话号码。"
            )
            return

        n = consume_number(cfg)
        await update.message.reply_photo(
            photo=photo.file_id,
            caption=caption(cfg, n, phone, amount)
        )
    except Exception as e:
        print("PHOTO ERROR:", repr(e), flush=True)
        await update.message.reply_text("识别失败，请重新发送照片。")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.message.media_group_id
    if not gid:
        await process_one_photo(update, context)
        return

    key = (update.effective_chat.id, gid)
    ALBUMS.setdefault(key, []).append(update)
    if key not in ALBUM_TASKS:
        ALBUM_TASKS[key] = asyncio.create_task(process_album(key, context))


async def process_album(key, context):
    await asyncio.sleep(ALBUM_WAIT)
    updates = ALBUMS.pop(key, [])
    ALBUM_TASKS.pop(key, None)
    if not updates:
        return

    updates.sort(key=lambda x: x.message.message_id)
    first = updates[0]
    cfg = state(context)

    try:
        text = await analyze_photo(first.message.photo[-1])
        phone = find_phone(text, cfg["code"])
        amount = find_amount(text)

        if not phone:
            await first.message.reply_text(
                f"没有识别到 {cfg['code']} 开头的电话号码。"
            )
            return

        n = consume_number(cfg)
        cap = caption(cfg, n, phone, amount)
        media = [
            InputMediaPhoto(
                media=u.message.photo[-1].file_id,
                caption=cap if i == 0 else None
            )
            for i, u in enumerate(updates[:3])
        ]
        await first.message.reply_media_group(media)
    except Exception as e:
        print("ALBUM ERROR:", repr(e), flush=True)
        await first.message.reply_text("相册识别失败，请重新发送。")


def video_frames(video_path, out_dir):
    # 约每 3 秒抽一帧，最多 20 帧
    pattern = str(Path(out_dir) / "frame_%03d.jpg")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", video_path,
            "-vf", "fps=1/3,scale='min(1280,iw)':-2",
            "-frames:v", "20",
            "-q:v", "3",
            pattern
        ],
        check=True,
        timeout=120
    )
    return sorted(Path(out_dir).glob("frame_*.jpg"))


def analyze_video_local(video_path, cfg):
    amount = ""
    with tempfile.TemporaryDirectory() as out_dir:
        for frame in video_frames(video_path, out_dir):
            try:
                with Image.open(frame) as im:
                    text = ocr(im)
            except Exception:
                continue
            if not amount:
                amount = find_amount(text)
            phone = find_phone(text, cfg["code"])
            if phone:
                return phone, amount
    return "", amount


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = state(context)
    try:
        video = update.message.video
        f = await video.get_file()
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "video.mp4")
            await f.download_to_drive(path)
            phone, amount = await asyncio.to_thread(analyze_video_local, path, cfg)

        if not phone:
            await update.message.reply_text(
                f"视频里没有识别到 {cfg['code']} 开头的电话号码。"
            )
            return

        n = consume_number(cfg)
        await update.message.reply_video(
            video=video.file_id,
            caption=caption(cfg, n, phone, amount)
        )
    except FileNotFoundError:
        await update.message.reply_text("服务器还没有安装 ffmpeg。")
    except Exception as e:
        print("VIDEO ERROR:", repr(e), flush=True)
        await update.message.reply_text("视频识别失败，请重新发送。")


def main():
    if not TOKEN:
        raise RuntimeError("没有设置 BOT_TOKEN")

    # 保存机器人里的国家、日期、模板和编号。
    # Railway 若挂载 /data Volume，重启/重新部署后也可保留。
    data_dir = Path("/data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        persistence_path = data_dir / "bot_state.pkl"
    except Exception:
        persistence_path = Path("bot_state.pkl")

    persistence = PicklePersistence(filepath=persistence_path)

    app = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("country", country_cmd))
    app.add_handler(CommandHandler("date", date_cmd))
    app.add_handler(CommandHandler("number", number_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.VIDEO, video_handler))

    print("Bot is running...", flush=True)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
