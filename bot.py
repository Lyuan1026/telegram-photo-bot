import os
import re
import io

from PIL import Image
import pytesseract
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")

# 现在识别 +1 的电话号码
# 以后例如要识别英国，可改成 +44 对应规则
PHONE_PATTERN = r"\+1[\s\-\(\)]*\d[\d\s\-\(\)]{7,15}\d"


def find_phone(text):
    matches = re.findall(PHONE_PATTERN, text)

    if not matches:
        return None

    phone = matches[0]

    # 整理 OCR 识别结果中的空格、括号、横线
    digits = re.sub(r"\D", "", phone)

    if digits.startswith("1"):
        return "+" + digits

    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "机器人已经启动。\n\n"
        "请直接发送一张包含电话号码的照片给我。"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("正在识别电话号码……")

    try:
        photo = update.message.photo[-1]
        telegram_file = await photo.get_file()

        data = await telegram_file.download_as_bytearray()

        image = Image.open(io.BytesIO(data))

        text = pytesseract.image_to_string(image)

        phone = find_phone(text)

        if not phone:
            await status.edit_text(
                "没有识别到 +1 开头的电话号码。\n"
                "请尽量发送清晰一点的照片。"
            )
            return

        caption = (
            f"电话：{phone}\n"
            f"国家：美国"
        )

        await status.delete()

        await update.message.reply_photo(
            photo=photo.file_id,
            caption=caption
        )

    except Exception as e:
        print("ERROR:", e)
        await status.edit_text("识别失败，请重新发送照片。")


def main():
    if not TOKEN:
        raise RuntimeError("没有设置 BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
