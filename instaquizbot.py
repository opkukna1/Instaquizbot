import logging
import os
import sys
import base64
import uuid
import json
import csv # CSV module ka upyog text ko parse karne mein madad karega
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler,
)
import firebase_admin
from firebase_admin import credentials, firestore, storage

# --- Firebase Setup ---
try:
    cred_json_str = base64.b64decode(os.environ.get("SERVICE_ACCOUNT_BASE64")).decode("utf-8")
    cred_json = json.loads(cred_json_str)
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred, {
        'storageBucket': f"{os.environ.get('FIREBASE_PROJECT_ID')}.appspot.com"
    })
    db = firestore.client()
    bucket = storage.bucket()
    print("Firebase successfully initialized.")
except Exception as e:
    print(f"Firebase initialization failed: {e}")
    sys.exit(1)

# --- Bot ki Basic Settings ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get('PORT', 8443))

# --- Naye Conversation States ---
(CHOOSING_ACTION,
 # Question states
 CHOOSING_UPLOAD_METHOD, GETTING_SUBJECT, GETTING_TOPIC, GETTING_POLL, GETTING_CSV_TEXT,
 # Note states
 ADDING_NOTE, GETTING_NOTE_TITLE, GETTING_NOTE_PDF,
 # Article states
 ADDING_ARTICLE, GETTING_ARTICLE_TITLE, GETTING_ARTICLE_TEXT,
 # Banner states
 GETTING_BANNER_IMAGE, CHOOSING_BANNER_TO_DELETE,
 # Update state
 GETTING_UPDATE_TEXT
) = range(15)


# --- Helper Functions ---
async def get_or_create_doc(collection_name, fields):
    """Firestore mein document dhoondhta hai ya banata hai."""
    collection_ref = db.collection(collection_name)
    query = collection_ref
    for key, value in fields.items():
        query = query.where(key, '==', value)
    
    docs = query.limit(1).get()
    if docs:
        return docs[0].id
    else:
        doc_ref = collection_ref.add(fields)
        return doc_ref[1].id

# --- Main Conversation Logic ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("➕ Add Questions", callback_data='add_question')],
        [InlineKeyboardButton("📄 Add Note (PDF)", callback_data='add_note')],
        [InlineKeyboardButton("✍️ Add Article", callback_data='add_article')],
        [InlineKeyboardButton("🖼️ Add Banner", callback_data='add_banner')],
        [InlineKeyboardButton("🗑️ Remove Banner", callback_data='remove_banner')],
        [InlineKeyboardButton("🔔 Set Latest Update", callback_data='set_update')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Agar user /start type kare, to naya message bhejein
    if update.message:
        await update.message.reply_text("Welcome, Admin! Please choose an action:", reply_markup=reply_markup)
    # Agar yeh callback se aaya hai (start_over), to purane message ko edit karein
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Please choose an action:", reply_markup=reply_markup)
    return CHOOSING_ACTION

# --- Question Adding Flow ---
async def add_question_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User se poochta hai ki question kaise add karna hai."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Forward Poll", callback_data='poll_upload')],
        [InlineKeyboardButton("Upload via CSV Text", callback_data='csv_upload')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("How would you like to add questions?", reply_markup=reply_markup)
    return CHOOSING_UPLOAD_METHOD

async def choose_upload_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Upload method ko save karta hai aur subject poochta hai."""
    query = update.callback_query
    await query.answer()
    context.user_data['upload_method'] = query.data # 'poll_upload' ya 'csv_upload'
    await query.edit_message_text("Let's add new questions. First, what is the subject? (e.g., History, Physics)")
    return GETTING_SUBJECT

async def receive_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subject_name = update.message.text.strip()
    context.user_data['subject_name'] = subject_name
    await update.message.reply_text(f"Great! Now, what is the topic within {subject_name}? (e.g., Modern History, Optics)")
    return GETTING_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    topic_name = update.message.text.strip()
    context.user_data['topic_name'] = topic_name
    
    if context.user_data.get('upload_method') == 'poll_upload':
        await update.message.reply_text("Excellent. Now please send the poll for this topic.")
        return GETTING_POLL
    elif context.user_data.get('upload_method') == 'csv_upload':
        await update.message.reply_html(
            "Excellent. Now, please send the questions as a text message.\n\n"
            "<b>Important:</b> Each question must be on a new line in this exact format:\n"
            "<code>Question,OptionA,OptionB,OptionC,OptionD,CorrectAnswer,Explanation</code>\n\n"
            "The explanation is optional."
        )
        return GETTING_CSV_TEXT

async def receive_poll_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    poll = update.message.poll
    if poll.type != 'quiz' or poll.correct_option_id is None:
        await update.message.reply_text("Error: This is not a valid quiz poll. Please try again.")
        return GETTING_POLL

    try:
        subject_id = await get_or_create_doc('subjects', {'name': context.user_data['subject_name']})
        topic_id = await get_or_create_doc('topics', {'name': context.user_data['topic_name'], 'subjectId': subject_id})

        question_data = {
            "question": poll.question, "options": [opt.text for opt in poll.options],
            "correctAnswer": poll.options[poll.correct_option_id].text, "explanation": poll.explanation or "N/A",
            "subjectId": subject_id, "topicId": topic_id, "timestamp": firestore.SERVER_TIMESTAMP,
        }
        db.collection('questions').add(question_data)
        await update.message.reply_text("✅ Success! Question has been saved.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")
    
    context.user_data.clear()
    await update.message.reply_text("What would you like to do next?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Main Menu", callback_data='start_over')]]))
    return CHOOSING_ACTION

async def receive_csv_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text_data = update.message.text.strip()
    lines = text_data.split('\n')
    
    saved_count = 0
    errors = []

    try:
        subject_id = await get_or_create_doc('subjects', {'name': context.user_data['subject_name']})
        topic_id = await get_or_create_doc('topics', {'name': context.user_data['topic_name'], 'subjectId': subject_id})

        for i, line in enumerate(lines):
            try:
                # Use csv reader to handle potential quotes in data
                parts = next(csv.reader([line]))
                parts = [p.strip() for p in parts]

                if not (6 <= len(parts) <= 7):
                    errors.append(f"Line {i+1}: Incorrect number of parts (commas). Expected 6 or 7, found {len(parts)}.")
                    continue

                question, optA, optB, optC, optD, correct_answer = parts[:6]
                explanation = parts[6] if len(parts) > 6 else "N/A"
                options = [optA, optB, optC, optD]

                if correct_answer not in options:
                    errors.append(f"Line {i+1}: The correct answer '{correct_answer}' is not one of the provided options.")
                    continue
                
                question_data = {
                    "question": question, "options": options, "correctAnswer": correct_answer,
                    "explanation": explanation, "subjectId": subject_id, "topicId": topic_id,
                    "timestamp": firestore.SERVER_TIMESTAMP,
                }
                db.collection('questions').add(question_data)
                saved_count += 1
            except Exception as line_error:
                errors.append(f"Line {i+1}: An unexpected error occurred. Skipping. ({line_error})")

        # Report to user
        report_message = f"✅ Process Complete!\n\nSuccessfully saved: {saved_count} questions."
        if errors:
            report_message += "\n\nThe following lines had errors and were skipped:\n- " + "\n- ".join(errors)
        await update.message.reply_text(report_message)

    except Exception as e:
        await update.message.reply_text(f"A major error occurred: {e}")
    
    context.user_data.clear()
    await update.message.reply_text("What would you like to do next?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Main Menu", callback_data='start_over')]]))
    return CHOOSING_ACTION


# (Stubs for other flows remain unchanged for brevity)
async def add_note_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.edit_message_text("Note flow not implemented in this version.")
async def add_article_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.edit_message_text("Article flow not implemented in this version.")
async def add_banner_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.edit_message_text("Banner flow not implemented in this version.")
async def remove_banner_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.edit_message_text("Banner flow not implemented in this version.")
async def set_update_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.edit_message_text("Update flow not implemented in this version.")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: await update.message.reply_text('Operation cancelled.'); return ConversationHandler.END


if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(start, pattern='^start_over$')],
        states={
            CHOOSING_ACTION: [
                CallbackQueryHandler(add_question_start, pattern='^add_question$'),
                CallbackQueryHandler(add_note_start, pattern='^add_note$'),
                CallbackQueryHandler(add_article_start, pattern='^add_article$'),
                CallbackQueryHandler(add_banner_start, pattern='^add_banner$'),
                CallbackQueryHandler(remove_banner_start, pattern='^remove_banner$'),
                CallbackQueryHandler(set_update_start, pattern='^set_update$'),
            ],
            # Question states
            CHOOSING_UPLOAD_METHOD: [CallbackQueryHandler(choose_upload_method, pattern='^(poll|csv)_upload$')],
            GETTING_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_subject)],
            GETTING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
            GETTING_POLL: [MessageHandler(filters.POLL, receive_poll_and_save)],
            GETTING_CSV_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_csv_and_save)],
            # ... other states for notes, articles, etc. would go here ...
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    
    print("Bot is starting webhook...")
    application.run_webhook(
        listen="0.0.0.0", port=PORT, url_path=TOKEN,
        webhook_url=f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    )
