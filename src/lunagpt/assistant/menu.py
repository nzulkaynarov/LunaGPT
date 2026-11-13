"""The persistent menu, the help texts and the welcome: what each role sees at the bottom of the chat."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from lunagpt.assistant.internet_desk import BTN_ASK_TIME, BTN_QUIZ
from lunagpt.assistant.memory_desk import BTN_STORY

# The menu: one button per part of life; what is inside opens as inline buttons.
BTN_DAY = "🗓 Мой день"
BTN_LISTS = "🛒 Списки"
BTN_MEMORY = "🧠 Память"
BTN_INTERNET = "⏳ Интернет"
BTN_FAMILY = "🏠 Семья"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = {BTN_DAY, BTN_LISTS, BTN_MEMORY, BTN_INTERNET, BTN_FAMILY, BTN_HELP}
# Inside those menus. The texts are also accepted as typed buttons, since older menus carried them.
BTN_PEOPLE = "👥 Люди"
BTN_INVITE = "➕ Пригласить"
BTN_PHRASE = "😺 Фраза для Луны"
# Menus stay on a phone until a message brings a new one, so the old buttons keep working until then.
OLD_BUTTONS = {
    "🔔 Напоминания": BTN_DAY,
    "🧠 Что ты помнишь": BTN_MEMORY,
    "⏳ Моё время": BTN_INTERNET,
    "🧠 Викторина +15 мин": BTN_QUIZ,
    "🐈 Про Луну": BTN_STORY,
}
OLD_SHARED = "📺 Общие устройства"
# Which part of Luna a menu button opens, for the usage counts.
MENU_FEATURES = {
    BTN_DAY: "day",
    BTN_LISTS: "lists",
    BTN_MEMORY: "memory",
    BTN_INTERNET: "internet",
    BTN_FAMILY: "family",
    BTN_HELP: "help",
    BTN_ASK_TIME: "internet",
    BTN_QUIZ: "quiz",
    BTN_PEOPLE: "people",
    BTN_INVITE: "people",
    BTN_STORY: "stories",
    BTN_PHRASE: "phrases",
}
SHARED_MOVED = (
    "📺 Общие устройства теперь в боте HomeSec, кнопка «📺 Общие устройства»: там те же действия. "
    "Я занимаюсь личными делами, а домашней сетью управляет HomeSec."
)
FAMILY_MENU = "🏠 Семья: люди и заявки, приглашения. Телевизор и другие общие устройства — в боте HomeSec."

MEMORY_NOTICE = " Запоминаю важное о тебе, посмотреть и удалить можно в «🧠 Память». Другие люди этого не видят."
FAMILY_NOTICE = " Чтобы запомнить для всех в семье, напиши: «запомни для семьи, что…»."
WHOSE = "👤 — видишь только ты, 🏠 — видит вся семья."
ASK_ANYTHING = "Просто напиши или скажи голосом, что нужно: вопрос, идея, перевод, свежие новости."
PHOTO_HINT = "📸 Пришли фото задачи, чека или текста, и я разберу.\n\n"
SCHOOL_HINT = "📚 Фото расписания с подписью «расписание» или «задали …» запомню и напомню вовремя.\n\n"
DAY_LINE = f"{BTN_DAY} — напоминания, поручения и утренняя сводка: «напомни завтра в 9…», «присылай каждый день…»\n"
LISTS_LINE = f"{BTN_LISTS} — 🏠 семейные и 👤 личные: «добавь молоко в покупки», «добавь в мой список…»\n"
MEMORY_LINE = f"{BTN_MEMORY} — 👤 что я помню о тебе, 🏠 общая память семьи и 🐈 истории про Луну\n"
ADULT_HELP = (
    f"{ASK_ANYTHING}\n\n{PHOTO_HINT}"
    + DAY_LINE
    + LISTS_LINE
    + MEMORY_LINE
    + f"{BTN_FAMILY} — люди и приглашения\n\n"
    + WHOSE
    + "\n\n"
    + MEMORY_NOTICE.strip()
    + FAMILY_NOTICE
)
HELP_TEXT = {
    "owner": ADULT_HELP,
    "adult": ADULT_HELP,
    "kid": (
        "Спрашивай что угодно: уроки, игры, код, идеи, свежие новости.\n\n"
        + PHOTO_HINT
        + SCHOOL_HINT
        + f"{BTN_INTERNET} — сколько осталось, попросить минут у родителей и 🎯 викторина\n"
        + f"{BTN_DAY} — напоминания, расписание уроков и домашка\n"
        + LISTS_LINE
        + MEMORY_LINE.rstrip("\n")
        + ", там же 😺 фраза для Луны\n\n"
        + WHOSE
        + "\n\n"
        + MEMORY_NOTICE.strip()
        + FAMILY_NOTICE
    ),
    "guest": (
        f"{ASK_ANYTHING}\n\n{PHOTO_HINT}{BTN_DAY} — напоминания и утренняя сводка\n"
        f"{BTN_MEMORY} — что я о тебе помню\n\n" + MEMORY_NOTICE.strip()
    ),
}
WELCOME = "<b>Добро пожаловать, {name}!</b> 🌙\n\n{help}"
BACK_AGAIN = "Привет, {name}! Я на связи. Напиши, что нужно, или выбери кнопку внизу."


def menu_layout(role: str) -> list[list[str]]:
    adults = [[BTN_DAY, BTN_LISTS], [BTN_MEMORY, BTN_FAMILY], [BTN_HELP]]
    layouts = {
        "owner": adults,
        "adult": adults,
        "kid": [[BTN_INTERNET, BTN_DAY], [BTN_LISTS, BTN_MEMORY], [BTN_HELP]],
        "guest": [[BTN_DAY, BTN_MEMORY], [BTN_HELP]],
    }
    return layouts[role]


def _inline(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def family_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_inline(BTN_PEOPLE, "fhp"), _inline(BTN_INVITE, "fhi")],
        ]
    )


def main_keyboard(role: str) -> ReplyKeyboardMarkup:
    """The persistent menu; server-side checks still decide what each role may do."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in menu_layout(role)],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Напиши Luna…",
    )
