import asyncio
import datetime
import io
import logging

from dateutil import parser as dateutil_parser
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginHiddenUser,
    MessageOriginUser,
    Update,
)
from telegram.error import NetworkError
from telegram.ext import ContextTypes

from bot import chart, db, ocr
from bot.config import ADMIN_TELEGRAM_ID, PLAYER_CODES, TIMEZONE, normalize_code
from bot.parser import parse_flashback_message, parse_flashback_screenshot

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Comandos disponibles:\n"
    "/soy <código> — vinculá tu usuario de Telegram a tu código de jugador "
    "(Se, Mb, Na, Ra, ²H)\n"
    "/tabla [año] — tabla de posiciones (default: año actual)\n"
    "/pendientes [código] — semanas que te faltan jugar, con el link\n"
    "/chatid — id de este chat (para configurar avisos)\n"
    "/ayuda — este mensaje\n\n"
    "Para cargar un puntaje, simplemente pegá el mensaje que comparte el "
    "juego de Flashback en el grupo, mandá una captura de pantalla del "
    "resultado, o pegá el texto de esa captura (en estos dos últimos casos "
    "te voy a preguntar a qué semana corresponde, porque no dice la fecha)."
)


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    result = parse_flashback_message(message.text)
    if result is not None:
        user = update.effective_user
        player = db.get_player_by_telegram_id(user.id) if user else None
        if player is None:
            await message.reply_text(
                "No te tengo vinculado a ningún jugador todavía. "
                "Usá /soy <código> (Se, Mb, Na, Ra, ²H) para vincularte, "
                "o pedile a un admin que lo haga con /vincular."
            )
            return

        db.upsert_score(
            player_code=player["code"],
            puzzle_date=result.puzzle_date,
            score=result.score,
            chat_id=update.effective_chat.id if update.effective_chat else None,
            message_id=message.message_id,
            raw_text=message.text,
        )

        pretty_date = datetime.date.fromisoformat(result.puzzle_date).strftime("%d/%m/%Y")
        await message.reply_text(
            f"✅ {player['code']} — {pretty_date}: {result.score} puntos registrados."
        )
        return

    # Not the share-text format — maybe it's the OCR'd text of the in-app
    # "you scored" summary card, pasted directly instead of as a screenshot.
    score = parse_flashback_screenshot(message.text)
    if score is None:
        return

    await _offer_score_date_picker(message, update.effective_user, score)


def _infer_current_puzzle_date(when: datetime.datetime) -> str:
    # New editions drop on Saturday (weekday() == 5), so "this week's
    # puzzle" is the most recent Saturday on or before `when`.
    days_since_saturday = (when.weekday() - 5) % 7
    saturday = when.date() - datetime.timedelta(days=days_since_saturday)
    return saturday.isoformat()


async def _offer_score_date_picker(message, user, score: int) -> None:
    """Ask which pending week a dateless score (from OCR or pasted text) is for."""
    player = db.get_player_by_telegram_id(user.id) if user else None
    if player is None:
        await message.reply_text(
            "No te tengo vinculado a ningún jugador todavía. "
            "Usá /soy <código> (Se, Mb, Na, Ra, ²H) para vincularte, "
            "o pedile a un admin que lo haga con /vincular."
        )
        return

    code = player["code"]
    sent_at = message.date.astimezone(TIMEZONE)
    current_puzzle_date = _infer_current_puzzle_date(sent_at)

    candidates = set(db.get_pending(code))
    if not db.has_score(code, current_puzzle_date):
        candidates.add(current_puzzle_date)
    sorted_candidates = sorted(candidates, reverse=True)[:10]

    if not sorted_candidates:
        await message.reply_text(
            f"Encontré un puntaje de {score} puntos, pero no "
            f"tengo ninguna semana pendiente para {code}. Usá /puntaje "
            f"{code} <fecha DD/MM/YYYY> {score} para cargarlo manualmente."
        )
        return

    uid = user.id
    buttons = [
        [
            InlineKeyboardButton(
                datetime.date.fromisoformat(d).strftime("%d/%m/%Y"),
                callback_data=f"flb_pick:{d}:{score}:{uid}",
            )
        ]
        for d in sorted_candidates
    ]
    buttons.append([InlineKeyboardButton("Cancelar", callback_data=f"flb_cancel:{uid}")])

    await message.reply_text(
        f"Encontré un puntaje de {score} puntos para {code}, pero no dice "
        "la fecha. ¿A qué semana corresponde?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.photo:
        return

    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    score = ocr.extract_score(image_bytes)
    if score is None:
        # Doesn't look like a Flashback score screenshot — stay quiet.
        return

    await _offer_score_date_picker(message, update.effective_user, score)


async def on_photo_score_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""

    if data.startswith("flb_cancel:"):
        _, uid_str = data.split(":", 1)
        if str(query.from_user.id) != uid_str:
            await query.answer("Este botón no es para vos.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text("Cancelado.")
        return

    if data.startswith("flb_pick:"):
        _, puzzle_date, score_str, uid_str = data.split(":", 3)
        if str(query.from_user.id) != uid_str:
            await query.answer("Este botón no es para vos.", show_alert=True)
            return

        player = db.get_player_by_telegram_id(query.from_user.id)
        if player is None:
            await query.answer()
            await query.edit_message_text(
                "Ya no estás vinculado a ningún jugador, no pude guardar el puntaje."
            )
            return

        score = int(score_str)
        db.upsert_score(
            player_code=player["code"],
            puzzle_date=puzzle_date,
            score=score,
            chat_id=update.effective_chat.id if update.effective_chat else None,
        )
        pretty_date = datetime.date.fromisoformat(puzzle_date).strftime("%d/%m/%Y")
        await query.answer()
        await query.edit_message_text(
            f"✅ {player['code']} — {pretty_date}: {score} puntos registrados."
        )
        return

    await query.answer()


async def cmd_soy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not context.args:
        await message.reply_text(
            "Uso: /soy <código> (Se, Mb, Na, Ra, ²H). Ejemplo: /soy Se"
        )
        return

    code = normalize_code(context.args[0])
    if code is None:
        await message.reply_text(
            f"Código inválido. Los códigos válidos son: {', '.join(PLAYER_CODES)}"
        )
        return

    player = db.get_player_by_code(code)
    if player["telegram_user_id"] is not None and player["telegram_user_id"] != user.id:
        await message.reply_text(
            f"El código {code} ya está vinculado a otro usuario. "
            "Pedile a un admin que lo corrija con /vincular."
        )
        return

    db.link_player(code, user.id, user.username)
    await message.reply_text(f"Listo, quedaste vinculado como {code}.")


async def cmd_vincular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if ADMIN_TELEGRAM_ID is None or user is None or user.id != ADMIN_TELEGRAM_ID:
        await message.reply_text("Este comando es solo para el admin.")
        return

    if not context.args:
        await message.reply_text(
            "Uso: /vincular <código> respondiendo (reply) al mensaje de la "
            "persona a vincular."
        )
        return

    code = normalize_code(context.args[0])
    if code is None:
        await message.reply_text(
            f"Código inválido. Los códigos válidos son: {', '.join(PLAYER_CODES)}"
        )
        return

    replied = message.reply_to_message
    if replied is None:
        await message.reply_text(
            "Tenés que responder (reply) al mensaje de la persona que querés "
            "vincular (puede ser un mensaje reenviado de ella)."
        )
        return

    origin = replied.forward_origin
    if isinstance(origin, MessageOriginUser):
        # Forwarded message: link the original sender, not whoever forwarded it.
        target = origin.sender_user
    elif isinstance(origin, MessageOriginHiddenUser):
        await message.reply_text(
            f"'{origin.sender_user_name}' tiene oculto quién reenvía sus mensajes "
            "en su configuración de privacidad de Telegram, así que no puedo "
            "obtener su usuario desde acá. Pedile que te escriba directamente o "
            "que corra /soy él mismo."
        )
        return
    else:
        target = replied.from_user

    if target is None:
        await message.reply_text(
            "No pude identificar a la persona de ese mensaje."
        )
        return

    db.link_player(code, target.id, target.username)
    await message.reply_text(f"Listo, vinculé a {target.first_name} como {code}.")


async def cmd_puntaje(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if ADMIN_TELEGRAM_ID is None or user is None or user.id != ADMIN_TELEGRAM_ID:
        await message.reply_text("Este comando es solo para el admin.")
        return

    if len(context.args) != 3:
        await message.reply_text(
            "Uso: /puntaje <código> <fecha DD/MM/YYYY> <puntaje>. "
            "Ejemplo: /puntaje Se 23/08/2026 42"
        )
        return

    code_arg, date_arg, score_arg = context.args

    code = normalize_code(code_arg)
    if code is None:
        await message.reply_text(
            f"Código inválido. Los códigos válidos son: {', '.join(PLAYER_CODES)}"
        )
        return

    try:
        puzzle_date = dateutil_parser.parse(date_arg, dayfirst=True).date().isoformat()
    except (ValueError, OverflowError):
        await message.reply_text("Fecha inválida. Usá el formato DD/MM/YYYY.")
        return

    try:
        score = int(score_arg)
    except ValueError:
        await message.reply_text("El puntaje tiene que ser un número entero.")
        return

    db.upsert_score(player_code=code, puzzle_date=puzzle_date, score=score)

    pretty_date = datetime.date.fromisoformat(puzzle_date).strftime("%d/%m/%Y")
    await message.reply_text(
        f"✅ {code} — {pretty_date}: {score} puntos registrados (por admin)."
    )


def _render_tabla(year: int) -> tuple[str, bytes] | None:
    standings = db.get_standings(year)
    if all(row["played"] == 0 for row in standings):
        return None

    header = f"{'Jugador':<8}{'Suma':>6}{'Prom':>7}{'Jug':>5}{'Vict':>6}"
    lines = [header, "-" * len(header)]
    for row in standings:
        lines.append(
            f"{row['code']:<8}{row['total']:>6}{row['avg']:>7.2f}"
            f"{row['played']:>5}{row['wins']:>6}"
        )

    caption = f"Tabla de posiciones {year}\n<pre>{chr(10).join(lines)}</pre>"

    weekly_scores = db.get_weekly_scores(year)
    png_bytes = chart.render_scores_heatmap(year, weekly_scores)
    return caption, png_bytes


async def cmd_tabla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    year = datetime.date.today().year
    if context.args:
        try:
            year = int(context.args[0])
        except ValueError:
            await message.reply_text("Uso: /tabla [año]")
            return

    rendered = _render_tabla(year)
    if rendered is None:
        await message.reply_text(f"No hay puntajes cargados para {year}.")
        return

    caption, png_bytes = rendered
    await message.reply_photo(
        photo=io.BytesIO(png_bytes),
        caption=caption,
        parse_mode="HTML",
    )


async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if context.args:
        code = normalize_code(context.args[0])
        if code is None:
            await message.reply_text(
                f"Código inválido. Los códigos válidos son: {', '.join(PLAYER_CODES)}"
            )
            return
    else:
        player = db.get_player_by_telegram_id(user.id) if user else None
        if player is None:
            await message.reply_text(
                "No sé quién sos. Usá /pendientes <código> o vinculate primero con /soy."
            )
            return
        code = player["code"]

    pending = db.get_pending(code)
    if not pending:
        await message.reply_text(f"{code} no tiene semanas pendientes. 🎉")
        return

    lines = [f"Semanas pendientes para {code}:"]
    for puzzle_date in pending:
        pretty = datetime.date.fromisoformat(puzzle_date).strftime("%d/%m/%Y")
        lines.append(f"- {pretty}: {db.interactive_url(puzzle_date)}")

    await message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(f"Chat id: {chat.id}")


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # No visible message — just server-side logging, so chat ids are
    # recoverable from journalctl without anyone typing /chatid.
    chat = update.effective_chat
    new_member = update.my_chat_member.new_chat_member
    logger.info(
        "Chat member update: chat_id=%s title=%r type=%s status=%s",
        chat.id,
        chat.title,
        chat.type,
        new_member.status,
    )


# Waits between attempts when the weekly announcement can't reach Telegram,
# ~4 hours in total. A single timeout at 9:00 used to skip the whole week.
ANNOUNCE_RETRY_DELAYS = [60, 5 * 60, 15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60]


async def _send_with_retry(send, what: str):
    for attempt, delay in enumerate([*ANNOUNCE_RETRY_DELAYS, None], start=1):
        try:
            return await send()
        except NetworkError:
            if delay is None:
                raise
            logger.warning(
                "No pude mandar %s (intento %d), reintento en %ds",
                what,
                attempt,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)


async def announce_new_edition(context: ContextTypes.DEFAULT_TYPE) -> None:
    # The share message's date is the puzzle's date (e.g. Saturday), but the
    # NYT interactive URL for that same puzzle is dated the day before.
    today = datetime.datetime.now(TIMEZONE).date()
    url = db.interactive_url(today.isoformat())
    pretty = today.strftime("%d/%m")
    await _send_with_retry(
        lambda: context.bot.send_message(
            chat_id=context.job.chat_id,
            text=f"🔮 Salió el Flashback de esta semana ({pretty}). Jugalo acá:\n{url}",
        ),
        "el aviso semanal",
    )

    rendered = _render_tabla(today.year)
    if rendered is not None:
        caption, png_bytes = rendered
        await _send_with_retry(
            lambda: context.bot.send_photo(
                chat_id=context.job.chat_id,
                photo=io.BytesIO(png_bytes),
                caption=caption,
                parse_mode="HTML",
            ),
            "la tabla semanal",
        )
