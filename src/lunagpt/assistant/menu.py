"""The persistent menu, the help texts and the welcome: what each role sees at the bottom of the chat."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from lunagpt.assistant.access import FAMILY_ROLES
from lunagpt.assistant.calendar_desk import BTN_ADD_EVENT
from lunagpt.assistant.internet_desk import BTN_ASK_TIME, BTN_CHORE, BTN_QUIZ
from lunagpt.assistant.list_desk import BTN_NEW_LIST
from lunagpt.assistant.memory_desk import BTN_STORY
from lunagpt.assistant.reminder_desk import BTN_NEW_REMINDER, BTN_NEW_TASK

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

# The help: what this role can do, screen by screen in the order of its menu, with one example for each thing.
ASK_ANYTHING = (
    "💬 Спрашивай что угодно, текстом или голосом: {examples}. Пока я пишу ответ, его можно остановить кнопкой "
    "«Стоп», а готовый — оценить реакцией 👍 или 👎."
)
ASK_EXAMPLES = {
    "kid": "«объясни дроби», «переведи на английский: я опоздаю»",
    "guest": "«переведи на английский: где вокзал?», «какие новости сегодня?»",
}
ADULT_EXAMPLES = "«переведи на английский: я опаздываю», «какие новости сегодня?»"
# A voice note or a round video is always answered aloud as well; a typed question only when asked to.
VOICE_HINT = (
    "🎙 На голосовое или кружок отвечу и текстом, и голосом, а написанное скажу вслух, если попросить: "
    "«расскажи голосом сказку» или «озвучь» после ответа."
)
PHOTO_HINT = "📸 Пришли фото задачи, чека или текста, и я разберу."
DAY_REMINDER = (
    f"• {BTN_NEW_REMINDER} — «напомни завтра в 9 позвонить бабушке»; "
    "когда напомню, можно отложить: «⏰ +10 мин» или «⏰ +1 час»"
)
DAY_TASK = f"• {BTN_NEW_TASK} — в срок сделаю сама и пришлю: «каждый день в 11 присылай 5 английских слов»"
DAY_SCHOOL = (
    "• 📚 Расписание и 📝 Домашка — пришли фото с подписью «расписание» "
    "или напиши «задали по математике № 125 к пятнице»"
)
DAY_CALENDAR = f"• 📅 Календарь — «добавь в календарь стоматолог в четверг в 15:00» или кнопка «{BTN_ADD_EVENT}»"
DAY_BRIEF = "• ☀️ Утренняя сводка — выбери время и «🏙 Город для погоды»"
LISTS_LINES = (
    "• «добавь молоко и хлеб в покупки», «вычеркни молоко», а в личный — «добавь в мой список позвонить врачу»",
    f"• {BTN_NEW_LIST} — назови его и выбери, для всей семьи он или только для тебя",
)
MEMORY_MINE = "• 👤 что я помню о тебе — другие этого не видят, лишнее можно удалить"
MEMORY_FAMILY = (
    "• 🏠 Общая память — «запомни для семьи, что бабушка приедет в субботу»",
    f"• {BTN_STORY} — повадки и смешные случаи нашей кошки",
)
INTERNET_LINES = (
    f"• {BTN_ASK_TIME} — выбери причину, родители решат",
    f"• {BTN_CHORE} — напиши или пришли фото, что сделал",
    f"• {BTN_QUIZ} — один вопрос в день",
)
FAMILY_LINES = (f"• {BTN_PEOPLE} — семья, гости и заявки", f"• {BTN_INVITE} — ссылка для нового человека")
WHOSE = "👤 — видишь только ты, 🏠 — видит вся семья."
GUEST_MEMORY = f"{BTN_MEMORY} — что я запомнила о тебе. Другие этого не видят, лишнее можно удалить."


def _day_block(role: str, calendar: bool) -> list[str]:
    lines = [BTN_DAY, DAY_REMINDER, DAY_TASK]
    if role == "kid":
        lines.append(DAY_SCHOOL)
    if calendar and role in FAMILY_ROLES:
        lines.append(DAY_CALENDAR)
    return [*lines, DAY_BRIEF]


def _memory_block(role: str) -> list[str]:
    if role not in FAMILY_ROLES:
        return [GUEST_MEMORY]
    lines = [BTN_MEMORY, MEMORY_MINE, *MEMORY_FAMILY]
    return [*lines, f"• {BTN_PHRASE} — придумай, что я пишу, пока думаю"] if role == "kid" else lines


def help_text(role: str, calendar: bool = False) -> str:
    """What this role can do, grouped by the screens of its menu, and nothing it cannot use. The calendar is
    named only when the owner has connected Google, since its button is not there otherwise."""
    blocks = {
        BTN_DAY: _day_block(role, calendar),
        BTN_LISTS: [f"{BTN_LISTS} — 🏠 семейные и 👤 личные", *LISTS_LINES],
        BTN_MEMORY: _memory_block(role),
        BTN_INTERNET: [f"{BTN_INTERNET} — сколько времени осталось", *INTERNET_LINES],
        BTN_FAMILY: [BTN_FAMILY, *FAMILY_LINES],
    }
    intro = "\n".join([ASK_ANYTHING.format(examples=ASK_EXAMPLES.get(role, ADULT_EXAMPLES)), VOICE_HINT, PHOTO_HINT])
    screens = ["\n".join(blocks[button]) for row in menu_layout(role) for button in row if button in blocks]
    footer = [WHOSE] if role in FAMILY_ROLES else []
    return "\n\n".join([intro, *screens, *footer])


WELCOME = "<b>Добро пожаловать, {name}!</b> 🌙\n\n{help}\n\nЭта подсказка всегда есть в «" + BTN_HELP + "»."
BACK_AGAIN = (
    "Привет, {name}! Я на связи. Напиши или скажи голосом, что нужно, или выбери кнопку внизу. "
    f"Что я умею — в «{BTN_HELP}»."
)


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
